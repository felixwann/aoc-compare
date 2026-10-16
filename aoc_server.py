"""AOC 参数抓取及本机对比服务，仅使用 Python 标准库。"""

import argparse
import errno
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
MAX_PAGE = 5 * 1024 * 1024
PRINT_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
FETCH_LOCK = threading.Lock()
DATABASE_FILE = ROOT / "aoc_parameters.sqlite3"
SCHEMA_FILE = ROOT / "001_create_aoc_products.sql"
FIELDS_SCHEMA_FILE = ROOT / "002_create_product_fields.sql"
EDITABLE_FIELDS = {"price": "价格", "purchase_url": "购买链接"}
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())
# 仅合并含义明确的名称；最大分辨率/分辨率、色域覆盖率/色域容积等保留区别。
ALIAS_GROUPS = {
    "面板类型": ["面板", "面板类型", "面板技术"],
    "型号": ["型号", "产品型号", "商品名称(机种名)", "机种名"],
    "屏幕宽高比": ["屏幕宽高比", "屏幕比例", "宽高比"],
    "接口": ["接口", "接口类型", "输入接口", "视频接口"],
    "显示颜色": ["显示颜色", "显示色数", "色数"],
    "外壳颜色": ["外壳颜色", "机身颜色"],
    "壁挂": ["壁挂", "壁挂规格", "壁挂尺寸"],
    "喇叭": ["喇叭", "扬声器", "内置扬声器"],
}


def name_key(value):
    return re.sub(r"[\s:：]+", "", unicodedata.normalize("NFKC", value)).casefold()


ALIASES = {name_key(alias): target for target, names in ALIAS_GROUPS.items() for alias in names}


def canonical_name(value):
    return ALIASES.get(name_key(value), unicodedata.normalize("NFKC", value).strip().rstrip(":"))


def comparable(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"(?<=\d)\s*[x×]\s*(?=\d)", "×", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s*([():/@,×])\s*", r"\1", value)


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.walk(tag)

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        if self.tag == "br":
            return "\n"
        value = "".join(child.text() if isinstance(child, Node) else child for child in self.children)
        if self.tag in {"p", "div", "li"}:
            value += "\n"
        return value


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def clean_text(node):
    return "\n".join(line for line in (re.sub(r"[^\S\n]+", " ", line).strip() for line in node.text().splitlines()) if line)


def validate_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or parts.hostname != "www.aocmonitor.com.cn"
            or parts.username or parts.password or parts.port not in {None, 80, 443}
            or not re.fullmatch(r"/product-\d+\.html", parts.path)):
        raise ValueError("仅支持 www.aocmonitor.com.cn/product-数字.html 产品链接")
    return "https://www.aocmonitor.com.cn" + parts.path


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, validate_url(newurl))


def fetch_html(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 AOCParameterCompare/1.0", "Accept-Encoding": "identity"})
    with build_opener(SafeRedirect()).open(request, timeout=25) as response:
        raw = response.read(MAX_PAGE + 1)
        if len(raw) > MAX_PAGE:
            raise ValueError("产品页面超过 5MB，已停止读取")
        charset = response.headers.get_content_charset()
    # 不使用 errors=replace，避免在乱码内容上解析参数。
    for encoding in dict.fromkeys(filter(None, [charset, "utf-8-sig", "gb18030", "utf-16"])):
        try:
            return raw.decode(encoding)
        except (UnicodeError, LookupError):
            pass
    raise ValueError("页面编码无法确认，未解析参数")


def nuxt_value(source, field):
    """只解码 JSON 字符串与简单变量引用，不执行网页 JavaScript。"""
    start = source.find("window.__NUXT__=")
    if start < 0:
        return None
    end = source.find("</script>", start)
    script = source[start:end if end >= 0 else len(source)]
    match = re.search(r"\b" + re.escape(field) + r'\s*:\s*("(?:\\.|[^"\\])*"|[A-Za-z_$][\w$]*)', script)
    if not match:
        return None
    token = match[1]
    if token.startswith('"'):
        return json.loads(token)
    # Nuxt 会把重复字符串提取成 IIFE 实参；仅接受 JSON 原生字面量。
    params = re.search(r"function\(([^)]*)\)", script)
    args_start = script.rfind("}}(")
    if not params or args_start < 0:
        return None
    decoder = json.JSONDecoder()
    position, arguments = args_start + 3, []
    try:
        for _ in params[1].split(","):
            while script[position].isspace():
                position += 1
            value, position = decoder.raw_decode(script, position)
            arguments.append(value)
            while script[position].isspace():
                position += 1
            if script[position] == ",":
                position += 1
        return dict(zip(params[1].split(","), arguments)).get(token)
    except (ValueError, IndexError):
        return None


def find_table(document):
    # 已记录的目标 DOM：#pane-attr > div > div.richBox > table；页面本身无需修改事件绑定。
    for pane in document.root.walk():
        if pane.attrs.get("id") != "pane-attr":
            continue
        for box in pane.walk():
            if "richBox" in box.attrs.get("class", "").split():
                table = next(box.walk("table"), None)
                if table:
                    return table, "#pane-attr .richBox table"
    # 用户提供的绝对 XPath，索引以同名兄弟元素为准。
    node = document.root
    for tag, index in [("html", 1), ("body", 1), ("div", 1), ("div", 1), ("div", 1),
                       ("div", 2), ("div", 2), ("div", 1), ("div", 2), ("div", 2),
                       ("div", 1), ("div", 1), ("table", 1)]:
        children = [child for child in node.children if isinstance(child, Node) and child.tag == tag]
        if len(children) < index:
            return None, None
        node = children[index - 1]
    return node, "用户提供的绝对 XPath"


def parse_table(table):
    attributes, notes, carried = [], [], {}
    for row in table.walk("tr"):
        # 不把嵌套表格的行重复计入外层表格。
        ancestor = row.parent
        while ancestor and ancestor.tag != "table":
            ancestor = ancestor.parent
        if ancestor is not table:
            continue
        cells = {col: data[0] for col, data in carried.items()}
        carried = {col: (data[0], data[1] - 1) for col, data in carried.items() if data[1] > 1}
        col = 0
        for cell in (child for child in row.children if isinstance(child, Node) and child.tag in {"td", "th"}):
            while col in cells:
                col += 1
            try:
                colspan = max(1, min(32, int(cell.attrs.get("colspan", "1"))))
                rowspan = max(1, min(100, int(cell.attrs.get("rowspan", "1"))))
            except ValueError:
                colspan, rowspan = 1, 1
            text = clean_text(cell)
            for offset in range(colspan):
                cells[col + offset] = text
                if rowspan > 1:
                    carried[col + offset] = (text, rowspan - 1)
            col += colspan
        values = [cells[index] for index in sorted(cells)]
        if len(values) < 2 or len(set(values)) == 1:
            if values and values[0]:
                notes.append(values[0])
            continue
        for index in range(0, len(values) - 1, 2):
            name, value = values[index:index + 2]
            if name:
                attributes.append({"name": name, "canonical_name": canonical_name(name), "value": value})
    return attributes, notes


def scrape_product(url):
    url = validate_url(url)
    source = fetch_html(url)
    document = Document(source)
    table, selector = find_table(document)
    if table is None:
        content = nuxt_value(source, "notice_content")
        if isinstance(content, str) and content:
            table = next(Document(content).root.walk("table"), None)
            selector = "window.__NUXT__.data[].goodsSkuDetail.notice_content"
    if table is None:
        raise ValueError("未找到参数表；站点结构或参数数据格式可能已变化，未用其他表格冒充参数")
    attributes, notes = parse_table(table)
    if not attributes:
        raise ValueError("参数表中没有可识别的名称/数值行")
    title = next(document.root.walk("title"), None)
    model = next((a["value"] for a in attributes if a["canonical_name"] == "型号"), None)
    return {"url": url, "model": model or (clean_text(title).split("-参数")[0] if title else url),
            "fetched_at": datetime.now(timezone.utc).isoformat(), "source": selector,
            "attributes": attributes, "notes": notes}


def compare_products(products):
    ordered = {}
    for product_index, product in enumerate(products):
        manual = [{"name": label, "canonical_name": label, "value": product.get("fields", {}).get(key, "")}
                  for key, label in EDITABLE_FIELDS.items() if key != "purchase_url"]
        for attribute in manual + product["attributes"]:
            label = attribute["canonical_name"]
            key = name_key(label)
            if key not in ordered:
                ordered[key] = {"key": key, "name": label, "cells": [[] for _ in products]}
            ordered[key]["cells"][product_index].append({"name": attribute["name"], "value": attribute["value"]})
    rows = list(ordered.values())
    for row in rows:
        row["values"] = ["\n".join(entry["value"] for entry in cell) if cell else None for cell in row["cells"]]
        values = row["values"]
        row["status"] = ("single" if len(products) < 2 else "missing" if any(value is None for value in values)
                         else "same" if len({comparable(value) for value in values}) == 1 else "different")
    return rows


def fetch_result(url):
    try:
        return {"product": scrape_product(url)}
    except HTTPError as error:
        return {"error": {"url": url, "message": f"AOC 返回 HTTP {error.code}"}}
    except (ValueError, URLError, TimeoutError, OSError) as error:
        return {"error": {"url": url, "message": str(error)}}


def product_id(url):
    # 同一个数字 ID 的 http/https、查询参数和前导零不产生重复记录。
    return str(int(re.search(r"/product-(\d+)\.html", validate_url(url))[1]))


def init_database():
    with CACHE_LOCK, closing(sqlite3.connect(DATABASE_FILE, timeout=30)) as connection:
        connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        connection.executescript(FIELDS_SCHEMA_FILE.read_text(encoding="utf-8"))


def read_cache():
    with CACHE_LOCK, closing(sqlite3.connect(DATABASE_FILE, timeout=30)) as connection:
        rows = connection.execute(
            "SELECT product_id, url, model, fetched_at, source, attributes, notes "
            "FROM products ORDER BY fetched_at, product_id").fetchall()
        fields = {}
        for identifier, name, value in connection.execute("SELECT product_id, field_name, value FROM product_fields"):
            fields.setdefault(identifier, {})[name] = value
    products = [{"id": row[0], "url": row[1], "model": row[2], "fetched_at": row[3],
                 "source": row[4], "attributes": json.loads(row[5]), "notes": json.loads(row[6]),
                 "fields": fields.get(row[0], {})}
                for row in rows]
    return {"products": products, "saved_at": max((p["fetched_at"] for p in products), default=None)}


def save_product(product):
    # 每款成功后单独提交；唯一主键及事务保证重复写入不会覆盖已有产品。
    with CACHE_LOCK, closing(sqlite3.connect(DATABASE_FILE, timeout=30)) as connection:
        with connection:
            cursor = connection.execute(
                "INSERT INTO products (product_id, url, model, fetched_at, source, attributes, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(product_id) DO NOTHING",
                (product_id(product["url"]), product["url"], product["model"], product["fetched_at"],
                 product["source"], json.dumps(product["attributes"], ensure_ascii=False),
                 json.dumps(product["notes"], ensure_ascii=False)))
            return cursor.rowcount == 1


def fetch_and_save(url):
    result = fetch_result(url)
    if "product" in result:
        try:
            if not save_product(result["product"]):
                return {"skipped": {"id": product_id(url), "url": url}}
        except (OSError, sqlite3.Error) as error:
            return {"error": {"url": url, "message": f"SQLite 保存失败：{error}"}}
    return result


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        # Windows 原生独占端口，禁止另一进程通过 SO_REUSEADDR 重复绑定。
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload, content_type="application/json; charset=utf-8"):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-AOC-Instance", self.server.instance_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {"/", "/aoc_compare.html"}:
            self.reply(200, (ROOT / "aoc_compare.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/cache":
            try:
                cached = read_cache()
                # 完整载入待对比列表，获取或打开页面都不自动加入对比。
                self.reply(200, {"products": cached["products"], "rows": [], "errors": [],
                                 "cache": {"file": DATABASE_FILE.name, "saved_at": cached["saved_at"],
                                           "total_products": len(cached["products"]), "loaded": True}})
            except (OSError, ValueError, UnicodeError, sqlite3.Error) as error:
                self.reply(500, {"error": f"读取 SQLite 失败：{error}"})
        else:
            self.reply(404, {"error": "页面不存在"})

    def do_POST(self):
        if self.path not in {"/api/fetch", "/api/compare", "/api/products/delete", "/api/products/update"}:
            return self.reply(404, {"error": "接口不存在"})
        # 服务仅允许本机同源页面发起请求。
        expected_host = f"127.0.0.1:{self.server.server_port}"
        allowed_hosts = {expected_host, f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in allowed_hosts:
            return self.reply(403, {"error": "仅允许本机访问"})
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed_hosts}:
            return self.reply(403, {"error": "不允许跨站请求"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("请求正文不能为空")
            if self.headers.get_content_type() != "application/json":
                return self.reply(415, {"error": "请发送 application/json"})
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须为对象")
            if self.path == "/api/products/update":
                identifier, fields = payload.get("id"), payload.get("fields")
                if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]+", identifier):
                    raise ValueError("需要有效产品 ID")
                if not isinstance(fields, dict) or not fields or set(fields) - EDITABLE_FIELDS.keys():
                    raise ValueError("仅支持修改价格和购买链接")
                cleaned = {}
                for name, value in fields.items():
                    if not isinstance(value, str):
                        raise ValueError("字段值必须为文本")
                    value = value.strip()
                    if name == "price" and value:
                        if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", value):
                            raise ValueError("价格须为非负金额，整数最多9位，小数最多2位；留空可清除")
                        whole, _, fraction = value.partition(".")
                        value = str(int(whole)) + "." + fraction.ljust(2, "0")
                    if name == "purchase_url" and value:
                        parts = urlsplit(value)
                        if (len(value) > 4096 or re.search(r"[\s\x00-\x1f\x7f]", value)
                                or parts.scheme not in {"http", "https"} or not parts.hostname
                                or parts.username or parts.password or parts.port == 0):
                            raise ValueError("购买链接须为有效的 HTTP/HTTPS 地址，不能包含空白或账号密码")
                    cleaned[name] = value
                identifier = str(int(identifier))
                # 单产品字段补丁；事务内加写锁，重复保存幂等，不覆盖未提交的其他字段。
                with CACHE_LOCK, closing(sqlite3.connect(DATABASE_FILE, timeout=30)) as connection:
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        if connection.execute("SELECT 1 FROM products WHERE product_id = ?", (identifier,)).fetchone() is None:
                            return self.reply(404, {"error": "产品不存在，请刷新列表"})
                        connection.executemany(
                            "INSERT INTO product_fields (product_id, field_name, value) VALUES (?, ?, ?) "
                            "ON CONFLICT(product_id, field_name) DO UPDATE SET value = excluded.value",
                            [(identifier, name, value) for name, value in cleaned.items()])
                        saved_fields = dict(connection.execute(
                            "SELECT field_name, value FROM product_fields WHERE product_id = ?", (identifier,)))
                return self.reply(200, {"id": identifier, "fields": saved_fields})
            if self.path == "/api/products/delete":
                identifier = payload.get("id")
                if (payload.get("confirmed") is not True or not isinstance(identifier, str)
                        or not re.fullmatch(r"[0-9]+", identifier)):
                    raise ValueError("删除需要有效产品 ID 和二次确认")
                # 只删除用户确认的一款；重复删除返回成功，不触及其他记录。
                with FETCH_LOCK, CACHE_LOCK, closing(sqlite3.connect(DATABASE_FILE, timeout=30)) as connection:
                    with connection:
                        connection.execute("DELETE FROM product_fields WHERE product_id = ?", (str(int(identifier)),))
                        connection.execute("DELETE FROM products WHERE product_id = ?", (str(int(identifier)),))
                return self.reply(200, {"deleted_id": str(int(identifier))})
            urls = payload.get("urls")
            if not isinstance(urls, list) or not urls or not all(isinstance(url, str) for url in urls):
                raise ValueError("urls 必须为非空产品链接列表")
            # 以数字 ID 去重；单条无效链接不会阻止其他有效产品获取。
            unique, errors = {}, []
            for url in urls:
                if not url.strip():
                    continue
                try:
                    identifier = product_id(url.strip())
                    unique.setdefault(identifier, f"https://www.aocmonitor.com.cn/product-{identifier}.html")
                except ValueError as error:
                    errors.append({"url": url, "message": str(error)})
            if not unique and not errors:
                raise ValueError("请输入产品链接")
        except (ValueError, UnicodeError) as error:
            return self.reply(400, {"error": str(error)})
        except (OSError, sqlite3.Error) as error:
            return self.reply(500, {"error": f"SQLite 操作失败：{error}"})
        try:
            # 同一服务内批次串行、批内并发 4 个，避免重复请求同时下载相同 ID。
            with FETCH_LOCK:
                existing = {product["id"] for product in read_cache()["products"]}
                skipped = [{"id": identifier, "url": url} for identifier, url in unique.items() if identifier in existing]
                pending = [url for identifier, url in unique.items() if identifier not in existing]
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(fetch_and_save, pending))
                errors.extend(result["error"] for result in results if "error" in result)
                skipped.extend(result["skipped"] for result in results if "skipped" in result)
                count = sum("product" in result for result in results)
                cached = read_cache()
            result = {"products": cached["products"], "rows": [], "errors": errors,
                      "downloaded": count, "skipped": skipped,
                      "cache": {"file": DATABASE_FILE.name, "saved": True,
                                "saved_at": cached["saved_at"], "total_products": len(cached["products"])},
                      "comparison_note": "仅规范大小写、全半角、空白及数字之间的 x/×；保留单位、限定条件、脚注和原始参数。缺项不视为相同。"}
        except (OSError, ValueError, UnicodeError, sqlite3.Error) as error:
            return self.reply(500, {"error": f"SQLite 操作失败：{error}"})
        with PRINT_LOCK:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        self.reply(200, result)


def main():
    parser = argparse.ArgumentParser(description="AOC 产品参数本机对比服务")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为 1–65535")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        server = ExclusiveHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        if error.errno in {errno.EADDRINUSE, errno.EACCES} or getattr(error, "winerror", None) in {10048, 10013}:
            print(f"启动失败：端口 {args.port} 已被占用或不允许绑定，本次程序未启动。请先关闭原服务，再重新运行。", file=sys.stderr)
        else:
            print(f"启动失败：{error}", file=sys.stderr)
        return 1
    try:
        init_database()
    except (OSError, sqlite3.Error) as error:
        server.server_close()
        print(f"启动失败：SQLite 初始化失败：{error}", file=sys.stderr)
        return 1
    server.instance_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{os.getpid()}"
    print(f"打开 http://127.0.0.1:{args.port}/?run={server.instance_id} ，Ctrl+C 停止。抓取结果以 JSON 打印到标准输出。", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
