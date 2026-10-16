-- SQLite 产品库初始化；可重复执行，不删除或覆盖已有数据。
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT NOT NULL PRIMARY KEY,
    url TEXT NOT NULL,
    model TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    attributes TEXT NOT NULL,
    notes TEXT NOT NULL
);
