# 分析流程手动测试

重启 FastAPI，在 <http://127.0.0.1:8000/docs> 的 `POST /api/v1/analyze` 依次提问。查看 `plan`（分析意图）和 `steps`（实际执行）；不要只看最终答案。

## 1. 不需要数据库的问题

```json
{"question":"什么是销售额环比？给我解释公式，不要查询数据库。"}
```

预期：`plan.question_type` 为 `concept`，`requires_sql=false`，`steps=[]`；回答解释 `(本期-上期)/上期 × 100%`，不编造本项目的数据。

## 2. 简单查询，不强制 Python

```json
{"question":"查询 agent_sales_sample 中 2026 年 3 月的销售额是多少？销售额按 quantity * unit_price 计算。"}
```

预期：`requires_sql=true`，至少有一条 `sql` 步，结果为 `11600`；没有必要强制 `python`。

## 3. 时间对比

```json
{"question":"agent_sales_sample 中 2026 年 2 月到 3 月销售额变化了多少？按 quantity * unit_price 计算，请说明对比基准和变化率。"}
```

预期：`plan.comparison` 指向 2 月到 3 月；SQL 数据应是 2 月 `17400`、3 月 `11600`；差额 `-5800`，3 月相对 2 月变化率约 `-33.33%`。计算可以来自正确的 SQL，也可以来自 Python Tool，关键是 `steps` 有可核对的依据。

## 4. 原因诊断：必须区分贡献与因果

```json
{"question":"agent_sales_sample 中为什么 2026 年 3 月销售额比 2 月低？先验证两个月总额，再按商品类别拆解降幅；销售额按 quantity * unit_price 计算。"}
```

预期：`question_type=diagnosis`，分析步骤包含先验证、再拆解。2 月总额 `17400`，3 月 `11600`，下降 `5800`；电脑下降 `4000`，手机下降 `1000`，配件下降 `800`。电脑贡献最大，但样本只展示类别销售变化，不能据此断言顾客偏好变化等根因。可有一条或多条 SQL，取决于查询是否同时覆盖总额和分类。

## 5. 已支付净销售额的业务口径

```json
{"question":"agent_test_orders 和 agent_test_items 中，2026 年第一季度每月已支付订单的净销售额是多少？只计 paid，净销售额=quantity*unit_price-discount_amount。"}
```

预期：`plan.metric` 体现已支付订单、折扣后的净销售额；SQL 应连接两表，过滤 `paid` 和第一季度。1 月 `10900`、2 月 `6000`、3 月 `4650`。如果漏掉状态或折扣，数值可能错误。

如果某题结果不符，把该题完整的 `plan`、`answer`、`steps` 和 `timings_ms` 发给我；不要发送 `.env` 或密码。
