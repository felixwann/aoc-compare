# AOC 参数抓取与对比

使用 Python 3.9 及以上版本，无需安装依赖。在本目录终端运行：

```powershell
python aoc_server.py
```

浏览器打开 http://127.0.0.1:8765 。端口被占用时运行 `python aoc_server.py --port 8766`，并打开相应端口。按 Ctrl+C 停止服务。

同一端口只允许一个服务实例：关闭地址/端口复用，Windows 使用 `SO_EXCLUSIVEADDRUSE` 独占绑定。再次运行会打印端口占用提示并以退出码 1 结束，不打印启动成功地址、不自动换端口、不终止原进程。更新脚本后先关闭旧服务，再启动新服务；明确指定不同端口仍可分别运行。

成功启动后终端打印带 `?run=运行标识` 的地址，HTTP 响应的 `X-AOC-Instance` 标识对应当前实例。使用本次打印的地址打开页面。HTML 每次从磁盘读取，页面与接口返回禁止缓存的响应头，前端请求也使用 `cache: 'no-store'`。已打开页面在修改代码或重启后需刷新。这里禁用的是 HTTP 缓存，本地 SQLite 参数库继续保留。

- 输入 AOC 产品链接，每行一个，不设获取条数上限，批内最多并发 4 个。点击“获取产品”只下载尚未保存的产品 ID，不立即对比。
- 成功获取的产品写入同目录 `aoc_parameters.sqlite3`，显示在下方“待对比产品”栏目。启动时数据库和 `products` 表不存在则自动创建，建表语句在 `001_create_aoc_products.sql`，可重复执行。
- 产品路径 `product-数字.html` 中的数字作为唯一 ID；http/https、查询参数及数字前导零不产生不同记录。已入库 ID 在请求官网前跳过，重复 ID 不覆盖旧记录。失败的产品不入库，下次可重试。
- 点击卡片“加入对比”才加入表格；表头“移除对比”仅取消当前选择，保留 SQLite 记录。当前选择只保存在页面内存，刷新后重新选择。
- 点击待对比卡片“删除”会二次确认；确认后仅删除该 ID 的 SQLite 记录，同时移除当前对比列。取消确认不发请求。删除后可以重新获取。
- 下次打开页面从 SQLite 载入全部已获取产品，不读取旧 JSON，不自动请求官网，也不自动加入对比。旧 JSON 文件不迁移、不删除；需要的数据通过产品链接重新获取。
- 每款成功后独立事务提交，产品 ID 主键防止重复写入；同一服务的获取批次串行，批内并发，删除与获取互斥。保存失败明确提示，不把未入库产品显示成成功下载。请确保脚本目录可写。
- HTTP 接口和终端输出仍使用 JSON 作为传输格式，参数列表在 SQLite 文本字段中序列化保存，不再读写 JSON 文件；页面保留当前对比的 JSON/来源查看，移除 JSON 下载按钮。HTTP 访问日志写入标准错误。
- 支持隐藏相同项、高亮不同项、参数名称/数值搜索、原始名称显示。缺失参数显示“未列出”，原表空值显示“原表为空”。
- 按参数名对齐，不依赖行顺序；`ALIAS_GROUPS` 集中维护明确的同义名称，如“面板”“面板类型”。未知名称保留，不做模糊猜测。不把“分辨率”直接当作“最大分辨率”，也不把色域覆盖率当作色域容积。
- 保留原始名称、原始数值、数据说明和脚注。比较时仅规范全半角、大小写、空白和数字间的 x/×，不推导产品性能，不擅自换算单位。同一参数多行（如合并单元格中的支架调节）完整合并展示，不覆盖前值。

## 抓取依据与限制

已查阅 [Python HTTP 服务](https://docs.python.org/3/library/http.server.html)、[urllib.request](https://docs.python.org/3/library/urllib.request.html)、[HTMLParser](https://docs.python.org/3/library/html.parser.html) 与 [MDN Fetch](https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch)。标准库原生支持请求、解析和本机服务；产品参数抽取、别名及对比逻辑需新增代码。未安装第三方依赖。

离线保存采用 [Python sqlite3](https://docs.python.org/3/library/sqlite3.html)、[SQLite CREATE TABLE](https://www.sqlite.org/lang_createtable.html) 与 [ON CONFLICT](https://www.sqlite.org/lang_conflict.html) 的原生能力：自动建表、参数绑定、事务及主键冲突跳过。删除二次确认使用 [MDN window.confirm](https://developer.mozilla.org/en-US/docs/Web/API/Window/confirm) 原生接口。待对比/当前对比状态及 ID 去重编排需新增代码，无需第三方依赖。

端口独占依据 [Python socketserver](https://docs.python.org/3.12/library/socketserver.html)、本机 Python 标准库源码和 [微软 SO_EXCLUSIVEADDRUSE 文档](https://learn.microsoft.com/en-us/windows/win32/winsock/using-so-reuseaddr-and-so-exclusiveaddruse)；浏览器缓存设置依据 [MDN Request.cache](https://developer.mozilla.org/en-US/docs/Web/API/Request/cache)。使用原生配置与绑定扩展，不引入进程管理依赖。

优先定位 `#pane-attr .richBox table`，再按用户提供的绝对 XPath 定位。2026-10-15 检查两个真实页面发现：下载的 HTML 没有该 DOM，Nuxt 产品组件通过 `goodsSkuDetail.notice_content` 渲染参数表，因此读取页面内嵌的同一份数据。只解码字符串和简单变量引用，不执行远程 JavaScript，不调用未确认的接口，也不使用其他表格冒充参数。

当前适用包含上述 DOM 或 Nuxt `notice_content` 参数表的页面；其他页面结构、`goods_attr_format` 动态列表、验证码或访问限制会明确报错，需依据实际页面补充支持。复杂嵌套表格不在当前支持范围。别名映射为保守人工规则，未收录的同义名称仍单独展示。

仅绑定 `127.0.0.1`，仅抓取指定 AOC 域名的产品路径，重定向也校验；获取条数不限、并发 4 个，每次网络连接超时 25 秒。该服务用于本机对比，不部署到公网。仅操作本地 SQLite 产品库，不修改历史 JSON 或现有 `display-comparison.html`。

## 文件

- `aoc_server.py`：HTTP 服务、AOC 抓取、表格解析和对比 JSON。
- `aoc_compare.html`：同源获取表单、待对比产品管理和参数对比表。
- `AOC使用说明.md`：启动、能力和边界说明。
- `001_create_aoc_products.sql`：带序号、可重复执行的 SQLite 建表文件。
- `aoc_parameters.sqlite3`：启动后自动创建的产品库；停止服务后可连同上述文件复制到其他电脑。
