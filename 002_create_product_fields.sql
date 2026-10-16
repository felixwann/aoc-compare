-- 产品手动维护字段；可重复执行，不修改既有官网参数或历史数据。
CREATE TABLE IF NOT EXISTS product_fields (
    product_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (product_id, field_name),
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);
