"""Keep source evidence in state; use bounded verified views for reports."""
import json
import re

from app.agent.context import is_followup


def relevant_schema(state, schema):
    lines = schema.splitlines()
    names = {line.split(": ", 1)[0] for line in lines if ": " in line}
    explicit = {name for name in names if re.search(
        r"(?<!\w)" + re.escape(name.removeprefix("public.")) + r"(?!\w)", state.question)}
    if explicit:
        chosen = explicit
    elif state.plan and state.plan.analysis_context:
        chosen = {state.plan.analysis_context.source_table}
    elif is_followup(state.question, state.analysis_context):
        chosen = {state.analysis_context.source_table}
    else:
        return schema  # Do not hide candidates when the intended table is ambiguous.
    subset = [line for line in lines if line.split(": ", 1)[0] in chosen]
    return "\n".join(subset) if subset else schema


def conversation_reference(state):
    """Keep question text; long historic numeric answers are not current evidence."""
    turns = state.conversation_history
    if not turns:
        return "暂无。"
    lines = []
    for index, turn in enumerate(turns):
        # Preserve clarification answers so a user's selection has its question context.
        clarification = turn.answer.startswith(("为了准确分析", "我检测到数据库中存在多份"))
        answer = turn.answer if clarification else turn.answer[:180] if index == len(turns) - 1 else "已完成；数字不可复用。"
        lines.append(f"历史问题：{turn.question}\n历史回答参考：{answer}")
    return "\n".join(lines)


def tool_reference(steps, *, report=False):
    entries = []
    verified_driver_sources = set()
    if report:
        for step in steps:
            try:
                value = json.loads(step.observation)
                if step.action == "python" and value.get("operation") == "driver_check" and value.get("result", {}).get("reconciled") is True:
                    verified_driver_sources.add(value.get("source_round"))
            except (ValueError, AttributeError):
                continue
    for step in steps:
        observation = step.observation
        if report:
            try:
                value = json.loads(observation)
                if step.round in verified_driver_sources and step.action == "sql":
                    observation = json.dumps({"row_count": len(value["rows"]), "truncated": False,
                        "note": "辅助记录已由 driver_check 与独立总额核对，报告使用下方核验计算；原始完整记录仍在本轮 steps。"}, ensure_ascii=False)
                elif step.action == "python" and value.get("operation") == "driver_check" and value.get("result", {}).get("reconciled") is True:
                    value["result"]["comparisons"] = [dict(pair, details=pair["details"][:3])
                                                        for pair in value["result"]["comparisons"]]
                    value["note"] = "每对比较仅呈现绝对销售额变化最大的三项辅助明细，其他明细仍保存在本轮 steps；不能猜测省略项。"
                    observation = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
        if not report and step.action == "sql":
            try:
                value = json.loads(observation)
                if isinstance(value, dict) and isinstance(value.get("rows"), list):
                    rows = value["rows"]
                    value = {**value, "rows": rows[:3], "full_row_count": len(rows),
                             "omitted_from_prompt": max(0, len(rows) - 3),
                             "note": "仅供工具选择的预览；完整结果仍保存在工具记录，不能据此生成未展示数值。"}
                    observation = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                pass
        entries.append(f"第 {step.round} 轮，工具：{step.action}\n输入：{step.tool_input}\n执行结果：{observation}")
    return "\n".join(entries) or "暂无工具调用记录。"
