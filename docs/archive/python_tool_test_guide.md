# V1.2 Python Analysis Tool 手动测试

使用已有的 `public.agent_sales_sample` 测试数据即可，无需导入新数据。重新启动 FastAPI 后，在 <http://127.0.0.1:8000/docs> 的 `POST /api/v1/analyze` 中提交下列问题。

每题预期至少有三轮：先 `sql`，再 `python`，最后 `finish`。在响应的 `steps` 中检查 `action` 顺序；`python` 步的 `tool_input` 是计算参数，`observation` 是 Python 算出的结果。

## 1. 月销售额环比

```json
{
  "question": "请查询 agent_sales_sample 中 2026 年 1、2、3 月的月销售额，按月份排序；然后必须用 Python Tool 的 percent_change 计算相邻月份的环比变化率。销售额 = quantity * unit_price。"
}
```

预期 SQL 月销售额：1 月 21000、2 月 17400、3 月 11600。Python 结果中：2 月相对 1 月约 `-17.14%`，3 月相对 2 月约 `-33.33%`；1 月没有前一月，变化率应为 `null`。

## 2. 月销售额描述统计

```json
{
  "question": "先查询 agent_sales_sample 中 2026 年 1 至 3 月每月销售额，每月一行；然后必须用 Python Tool 的 summary 计算这三个月销售额的总和、均值、中位数、最小值和最大值。销售额 = quantity * unit_price。"
}
```

预期 Python 结果：`count=3`、总和 `50000`、均值约 `16666.67`、中位数 `17400`、最小值 `11600`、最大值 `21000`。

## 3. 类别占比

```json
{
  "question": "先查询 agent_sales_sample 中 2026 年第一季度各商品类别的销售额，每个类别一行；然后必须用 Python Tool 的 share 计算各类别占总销售额的百分比。销售额 = quantity * unit_price。"
}
```

预期 Python 结果：电脑 `48%`，手机 `40%`，配件 `12%`；总销售额 `50000`。

如果答案数值正确，但 `steps` 没有 `action: "python"`，表示模型自己计算了结果，没有真正调用 Python Tool，这题仍算未通过。请将该题完整的 `answer` 和 `steps` 发给我排查。
