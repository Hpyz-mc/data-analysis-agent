# V2.1 归因分析手动测试

继续使用已经导入的 `public.agent_sales_sample`，不需要新建表或插入数据。重启 FastAPI，在 <http://127.0.0.1:8000/docs> 的 `POST /api/v1/analyze` 中提交：

```json
{
  "question": "请分析 agent_sales_sample 中 2026 年 3 月比 2 月销售额下降的维度贡献。销售额按 quantity * unit_price 计算。先独立查询 2、3 月每月总销售额，再查询这两个月按 category 分组的销售额，必须调用 Python Tool 的 attribution 核对明细与总额并计算各类别的变化额和对总降幅的贡献率。不要把变化贡献说成因果根因。"
}
```

预期 `plan.python_operation` 为 `attribution`。`steps` 至少包含两次 `sql`（一次每月总额，一次月份×类别明细），之后是一次 `python`。Python 工具输入的 `total_source_round` 应指向总额 SQL，`source_round` 应指向类别明细 SQL。

按样例数据，2 月总额是 `17400`，3 月总额是 `11600`，总变化为 `-5800`。Python 结果应包含：

| 类别 | 2 月 | 3 月 | 变化额 | 对总降幅的贡献率 |
| --- | ---: | ---: | ---: | ---: |
| 电脑 | 9000 | 5000 | -4000 | 约 68.97% |
| 手机 | 6000 | 5000 | -1000 | 约 17.24% |
| 配件 | 2400 | 1600 | -800 | 约 13.79% |

`reconciled` 应为 `true`：2 月类别合计等于 17400，3 月类别合计等于 11600，三个变化额合计等于 -5800。答案可以说“电脑类别贡献了最大降幅”，不能说“顾客不喜欢电脑导致下降”。

如果 `answer` 数值对了，但没有 `attribution` 工具步骤或 `reconciled` 不是 `true`，就没有通过这次测试。把响应中的 `plan`、`answer`、`steps` 发给我排查；不要发送 `.env` 或密码。
