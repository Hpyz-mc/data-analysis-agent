-- 在你用于测试的 PostgreSQL 数据库中执行此文件。
-- 此脚本只创建 public.agent_sales_sample，不修改其他业务表。

CREATE TABLE IF NOT EXISTS public.agent_sales_sample (
    id integer PRIMARY KEY,
    order_date date NOT NULL,
    region text NOT NULL,
    category text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(12, 2) NOT NULL CHECK (unit_price >= 0)
);

INSERT INTO public.agent_sales_sample
    (id, order_date, region, category, quantity, unit_price)
VALUES
    (1, '2026-01-08', '华东', '电脑', 2, 5000.00),
    (2, '2026-01-13', '华南', '手机', 3, 3000.00),
    (3, '2026-01-20', '华北', '配件', 10, 200.00),
    (4, '2026-02-06', '华东', '电脑', 2, 4500.00),
    (5, '2026-02-14', '华南', '手机', 2, 3000.00),
    (6, '2026-02-22', '华北', '配件', 12, 200.00),
    (7, '2026-03-04', '华东', '电脑', 1, 5000.00),
    (8, '2026-03-16', '华南', '手机', 2, 2500.00),
    (9, '2026-03-24', '华北', '配件', 8, 200.00)
ON CONFLICT (id) DO NOTHING;

-- 供人工核对：销售额 = quantity * unit_price。
-- 2026-01: 21000；2026-02: 17400；2026-03: 11600。
-- 按类别汇总：电脑 24000；手机 20000；配件 6000。
