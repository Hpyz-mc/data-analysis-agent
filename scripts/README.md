# 手动验收脚本

在项目根目录运行，使用已启动的本机服务 `http://127.0.0.1:8001`。这些脚本会创建会话、调用真实模型并产生费用，不属于 CI 或单元测试。测试前需通过 API 加载内置合成表 `demo_ecommerce_orders`；正式 UI 隐藏该表。

```bash
curl -X POST http://127.0.0.1:8001/api/v1/datasets/preset
python -m scripts.accept_model_policy
```

`accept_model_policy` 检查 4 轮真实分析、流式结果、图表和历史保存，结果写入 `outputs/model_policy_acceptance.json`。

20 轮 Token 与多轮追问基准按需运行：

```bash
python -m scripts.token_benchmark
```

结果写入 `outputs/token_benchmark_20_turns.json`。每次运行会覆盖对应 JSON；复测前自行保存旧结果。原始结果可能包含问题、回答和查询记录，不应直接公开。历史报告见 [归档索引](../docs/archive/README.md)。
