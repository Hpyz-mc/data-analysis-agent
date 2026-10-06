# V1.1 进阶测试指南

这份数据用于检查 Agent 是否能读取其他表、连接多张表、处理业务口径，以及面对无法回答或错误 SQL 时保持诚实。由你在本机运行测试。

## 1. 导入数据

先看终端提示符。如果是 `agent_db=>` 或 `agent_db->`，说明你已经在 PostgreSQL 内部；先输入 `\r` 清除未完成的命令，再输入 `\q` 返回普通终端。不要在 `agent_db=>` 后输入 `psql -d agent_db`。

在普通终端中进入 PostgreSQL，使用有建表权限的账号（Homebrew 本机安装通常是当前 macOS 用户；不要使用只读的 `agent_reader`）：

```bash
psql -d agent_db
```

进入后先执行 `SELECT current_user;`，确认不是 `agent_reader`。看到 `agent_db=#` 后输入：

```text
\i examples/sql/advanced_test_data.sql
```

授权 Agent 使用的只读账号：

```sql
GRANT USAGE ON SCHEMA public TO agent_reader;
GRANT SELECT ON public.agent_test_customers, public.agent_test_orders, public.agent_test_items TO agent_reader;
```

如你的账号不是 `agent_reader`，把授权语句中的名字替换掉。核对数量：

```sql
SELECT COUNT(*) FROM public.agent_test_customers; -- 4
SELECT COUNT(*) FROM public.agent_test_orders;    -- 8
SELECT COUNT(*) FROM public.agent_test_items;     -- 10
```

输入 `\q` 退出。数据库已运行时无需重新启动 PostgreSQL；如果 FastAPI 正在运行，重新发起请求即可读取新表结构。

## 2. 通过 `/docs` 测试 Agent

打开 <http://127.0.0.1:8000/docs>，在 `POST /api/v1/analyze` 中依次提交以下问题。每次都观察 `steps` 中的 `tool_input` 和 `observation`，确认 Agent 真的查询了正确的表。

### A. 其他表：客户区域分布

```json
{"question":"只查询 agent_test_customers 表：每个区域有多少位客户？"}
```

预期：华东 2 位，华南 1 位，华北 1 位。

### B. 跨表查询：按月净销售额

```json
{"question":"连接 agent_test_orders 和 agent_test_items，统计 2026 年 1 至 3 月每月的已支付订单净销售额。只计 status='paid'，净销售额按 quantity * unit_price - discount_amount 计算；排除取消、退款和 4 月订单。"}
```

预期：1 月 10900.00，2 月 6000.00，3 月 4650.00；合计 21550.00。注意 3 月 31 日订单应计入，4 月 1 日订单不计入。

### C. 跨表查询：类别排名

```json
{"question":"连接 agent_test_orders 和 agent_test_items，计算 2026 年第一季度已支付订单各商品类别的净销售额，按从高到低排序。净销售额为 quantity * unit_price - discount_amount。"}
```

预期：电脑 10500.00，手机 9600.00，配件 1450.00。退款订单中的电脑 7000 和取消订单中的手机 3000 不应计入。

### D. 三表查询：包括没有订单的客户

```json
{"question":"连接 agent_test_customers、agent_test_orders、agent_test_items，列出每位客户在 2026 年第一季度已支付订单的净销售额。没有已支付订单的客户也要列出为 0；净销售额为 quantity * unit_price - discount_amount。"}
```

预期：小林 10900.00，小王 9900.00，小陈 750.00，小赵 0.00。查询应保留没有订单的小赵。

### E. 不存在的表

```json
{"question":"请查询 public.agent_test_inventory 中当前库存最低的商品。"}
```

预期：不能编造库存数据。Agent 可以说明当前没有该表，或尝试查询后报告数据库错误；答案中不应出现虚构的库存数量。

## 3. 直接检查 SQL Tool 的异常处理

在项目根目录的 PyCharm Terminal 输入 `python` 进入交互模式：

```python
from app.tools.sql_tool import SQLTool
tool = SQLTool()
tool.run("DELETE FROM public.agent_test_orders")
```

预期：抛出 `SQLToolError`，提示只允许 `SELECT`；表内仍应有 8 条订单。

再试一个不存在的字段：

```python
tool.run("SELECT made_up_column FROM public.agent_test_orders")
```

预期：抛出 `SQLToolError`，说明字段不存在。输入 `exit()` 退出交互模式。

这两项是工具级测试，不要把写入指令当作正常分析问题交给 Agent；Agent 的接口只接收自然语言问题。
