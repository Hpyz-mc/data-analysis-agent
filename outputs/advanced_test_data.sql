-- V1.1 进阶测试数据：客户、订单、订单明细三张表。
-- 请在测试用 agent_db 中，用有建表权限的账号执行。
-- 只创建/写入 agent_test_* 表，不修改 agent_sales_sample 或其他业务表。
-- 重复执行不会重复插入相同 ID；不会覆盖已有行。

CREATE TABLE IF NOT EXISTS public.agent_test_customers (
    customer_id integer PRIMARY KEY,
    customer_name text NOT NULL,
    region text NOT NULL
);

CREATE TABLE IF NOT EXISTS public.agent_test_orders (
    order_id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES public.agent_test_customers(customer_id),
    order_date date NOT NULL,
    status text NOT NULL CHECK (status IN ('paid', 'cancelled', 'refunded'))
);

CREATE TABLE IF NOT EXISTS public.agent_test_items (
    item_id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES public.agent_test_orders(order_id),
    category text NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(12, 2) NOT NULL CHECK (unit_price >= 0),
    discount_amount numeric(12, 2) NOT NULL DEFAULT 0
        CHECK (discount_amount >= 0 AND discount_amount <= quantity * unit_price)
);

INSERT INTO public.agent_test_customers
    (customer_id, customer_name, region)
VALUES
    (1, '小林', '华东'),
    (2, '小王', '华南'),
    (3, '小陈', '华东'),
    (4, '小赵', '华北')
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO public.agent_test_orders
    (order_id, customer_id, order_date, status)
VALUES
    (101, 1, '2026-01-05', 'paid'),
    (102, 2, '2026-01-20', 'paid'),
    (103, 1, '2026-02-10', 'paid'),
    (104, 3, '2026-02-28', 'cancelled'),
    (105, 2, '2026-03-03', 'paid'),
    (106, 3, '2026-03-15', 'refunded'),
    (107, 1, '2026-04-01', 'paid'),
    (108, 3, '2026-03-31', 'paid')
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO public.agent_test_items
    (item_id, order_id, category, quantity, unit_price, discount_amount)
VALUES
    (1001, 101, '电脑', 1, 5000.00, 500.00),
    (1002, 101, '配件', 2, 200.00, 0.00),
    (1003, 102, '手机', 2, 3000.00, 0.00),
    (1004, 103, '电脑', 1, 6000.00, 0.00),
    (1005, 104, '手机', 1, 3000.00, 0.00),
    (1006, 105, '手机', 1, 4000.00, 400.00),
    (1007, 105, '配件', 3, 100.00, 0.00),
    (1008, 106, '电脑', 1, 7000.00, 0.00),
    (1009, 107, '电脑', 1, 8000.00, 0.00),
    (1010, 108, '配件', 5, 150.00, 0.00)
ON CONFLICT (item_id) DO NOTHING;

-- 正确口径：净销售额 = quantity * unit_price - discount_amount。
-- 统计 2026 年第一季度时，只计 paid，日期 >= 2026-01-01 且 < 2026-04-01。
-- cancelled、refunded 和 4 月 1 日的订单不计入第一季度已支付净销售额。
