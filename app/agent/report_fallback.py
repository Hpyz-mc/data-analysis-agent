"""Bounded evidence receipt, never a substitute for a completed model report."""
import json
from app.agent.report_presentation import driver_evidence


def _cell(value):
    text = "—" if value is None else str(value)
    return text[:200].replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _table(headers, rows):
    return "\n".join([
        "| " + " | ".join(_cell(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(_cell(item) for item in row) + " |" for row in rows),
    ])


def partial_report(agent):
    parts = ["## 本轮仅部分完成", "以下仅保留已执行的查询与核验结果，不代表完整分析报告。"]
    context = agent.plan.analysis_context if agent.plan else None
    if context:
        parts.append(f"数据表：`{context.source_table}`；指标：{_cell(context.metric_label)}。")
        for label, period in (("基期", context.baseline), ("当期/查询范围", context.current)):
            if period:
                parts.append(f"{label}：{period.start.isoformat()} 至 {period.end.isoformat()}（不含结束日）")
        if context.filters:
            parts.append("筛选：" + "；".join(
                f"{_cell(item.column)} = {', '.join(_cell(v) for v in item.values)}"
                for item in context.filters))
    observations = []
    for step in agent.steps:
        try:
            observation = json.loads(step.observation)
        except (ValueError, TypeError):
            continue
        if isinstance(observation, dict):
            observations.append((step, observation))
    changes = next((obj.get("result") for step, obj in observations
                    if step.action == "python" and obj.get("operation") == "percent_change"
                    and isinstance(obj.get("result"), list)), None)
    if changes:
        parts.append("### 已完成：返回时期的变化\n\n" + _table(
            ["时期", "指标值", "较上一返回时期变化额"],
            [[p.get("label"), p.get("value"), p.get("difference")] for p in changes[:30]]))
        if len(changes) > 30:
            parts.append("这里只展示前 30 个时期，完整返回结果见执行证据；不是全期汇总。")
    else:
        for step, obj in observations:
            rows = obj.get("rows")
            if step.action != "sql" or obj.get("truncated") or not isinstance(rows, list) or not rows:
                continue
            if not all(isinstance(row, dict) for row in rows):
                continue
            headers = list(rows[0])[:8]
            if headers:
                parts.append("### 已完成：查询结果预览\n\n" + _table(
                    headers, [[row.get(key) for key in headers] for row in rows[:5]]))
                parts.append(f"本条查询返回 {len(rows)} 行；这里只显示最多 5 行、8 列，不据预览追加统计结论。")
                break
    for step, obj in observations:
        result = obj.get("result")
        if step.action == "python" and obj.get("operation") == "driver_check" and isinstance(result, dict) and result.get("reconciled") is True:
            parts.append(driver_evidence(result))
            continue
        if step.action != "python" or obj.get("operation") not in {"attribution", "sequential_attribution"}:
            continue
        if not isinstance(result, dict) or result.get("reconciled") is not True:
            continue
        pairs = result.get("comparisons", []) if obj["operation"] == "sequential_attribution" else [result]
        for pair in pairs[:5]:
            parts.append(f"### 已核对：{_cell(pair.get('baseline_period'))} → {_cell(pair.get('current_period'))}\n\n"
                         f"独立总额：{_cell(pair.get('baseline_total'))} → {_cell(pair.get('current_total'))}；"
                         f"净变化：{_cell(pair.get('total_change'))}。\n\n" + _table(
                             ["维度", "变化额", "占净变化贡献（%）"],
                             [[row.get("dimension"), row.get("change"), row.get("contribution_percent")]
                              for row in pair.get("details", [])[:3]]))
        parts.append("每个比较仅展示绝对变化最大的前三项，最多展示五组比较；完整明细见执行证据。贡献不是因果证明，净变化为零时贡献率未定义。")
    if agent.charts:
        parts.append("### 已生成的图表\n\n" + "\n\n".join(
            f"![已完成的分析图表]({chart})" for chart in agent.charts))
    parts.append("### 未完成的要求\n\n" + (agent.feedback or "未能生成最终报告，尚未获得全部必要证据。"))
    parts.append("本轮标记为未完成，不会作为后续分析的已完成证据；不会自动重跑查询或继续调用模型。")
    return "\n\n".join(parts)
