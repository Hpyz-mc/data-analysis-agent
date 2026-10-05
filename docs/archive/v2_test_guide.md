# V2 手动测试指南

先在 PyCharm Terminal 安装新增依赖，再重启服务：

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000/docs>，调用 `POST /api/v1/analyze`。继续使用已经导入的 `public.agent_sales_sample` 数据，不需要重新插入。观察 `plan`、`steps`、`charts` 和 `timings_ms`。

## 1. Pandas 检查查询结果

```json
{"question":"查询 agent_sales_sample 的全部 9 条测试记录，然后必须用 Python Tool 的 profile 检查这次 SQL 结果的行数、各列缺失值、空字符串和重复行。"}
```

预期：`steps` 先 `sql` 后 `python`，Python 结果的 `operation=profile`、`row_count=9`、各列缺失值 `0`、空字符串 `0`、重复行 `0`。这不是全数据库质量报告。

## 2. 月销售额折线图

```json
{"question":"查询 agent_sales_sample 中 2026 年 1 至 3 月每个月的销售额，按月份排序，销售额按 quantity * unit_price 计算，并生成一张折线图。"}
```

预期：1 月 `21000`、2 月 `17400`、3 月 `11600`。`steps` 中有 `sql` 和 `chart`；`plan.chart_type=line`；响应的 `charts` 至少有一个 `/api/v1/charts/xxx.png` 路径。将该路径接在 `http://127.0.0.1:8000` 后，在浏览器打开应看到折线图。

## 3. 类别销售额柱状图

```json
{"question":"统计 agent_sales_sample 中 2026 年第一季度各商品类别的销售额，销售额按 quantity * unit_price 计算，并生成柱状图。"}
```

预期：电脑 `24000`、手机 `20000`、配件 `6000`；`plan.chart_type=bar`，`charts` 中有可打开的 PNG 链接。

## 4. 错误处理

```json
{"question":"查询 public.agent_test_inventory 的库存数量；如果这个表不存在，请明确说明无法得出库存结论，不要编造数据。"}
```

预期：不能出现虚构库存数。若 SQL 报错，`steps` 应记录错误；同一条失败 SQL 不会被原样再次执行，且最多执行两条失败 SQL。也可能由 Agent 根据表结构直接发现表不存在，再尝试查询后说明无法完成。

如果结果不符合预期，把对应请求的 `plan`、`answer`、`steps`、`charts` 发给我，不要发送 `.env` 或密码。
