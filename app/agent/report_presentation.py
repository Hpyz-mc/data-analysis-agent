"""Fill missing evidence coverage without more model calls; not a prose fact checker."""
import json
import re
from decimal import Decimal, InvalidOperation


def cell(value):
    return str(value if value is not None else "未定义").replace("|", "\\|").replace("\n", " ")


def numbers(text):
    values = set()
    for token in re.findall(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?", text):
        try:
            values.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            pass
    return values


def covers(text, values):
    present = numbers(text)
    return all(Decimal(str(value)) in present for value in values if value is not None)


def driver_evidence(result):
    parts = ["### 已核对的原因查证"]
    for pair in result.get("comparisons", []):
        parts.append(f"**{cell(pair['baseline_period'])} → {cell(pair['current_period'])}**")
        if result.get("order_metric_available"):
            parts.append(f"订单量：{cell(pair['baseline_orders'])} → {cell(pair['current_orders'])}；"
                         f"客单价：{cell(pair['baseline_aov'])} → {cell(pair['current_aov'])}（总销售额/总订单量）。")
            if pair.get("effects_reconciled"):
                parts.append(f"订单量变化效应：{cell(pair['order_effect'])}；客单价变化效应：{cell(pair['aov_effect'])}；"
                             f"两项已核对销售额变化 {cell(pair['sales_change'])}（显示舍入差额 {cell(pair['rounding_residual'])}）。")
            elif pair.get("limitation"):
                parts.append(pair["limitation"])
        # Prefer the largest drag on a decline, not the largest offsetting growth.
        details = pair.get("details", [])
        sign = -1 if Decimal(pair["sales_change"]) < 0 else 1
        drivers = [row for row in details if Decimal(row.get("sales_change", "0")) * sign > 0]
        top = (drivers or details)[0] if details else None
        if top:
            before, after = top.get("baseline_supply", {}), top.get("current_supply", {})
            if before or after:
                def supply_text(record):
                    text = []
                    if record.get("stockout_min") is not None and record.get("stockout_max") is not None:
                        text.append(f"缺货记录值 {cell(record['stockout_min'])}–{cell(record['stockout_max'])}")
                    if record.get("supply_statuses"):
                        text.append("供货记录：" + "、".join(map(str, record["supply_statuses"])))
                    return "；".join(text) or "该范围没有可用供货记录"
                parts.append(f"主要贡献项 **{cell(top['dimension'])}** 的同范围辅助记录：基期 {supply_text(before)}；当期 {supply_text(after)}。")
    parts.append("这是对称机械分解与关联查证，不是因果证明；客单价包含订单结构影响，不等于商品售价；缺货记录值不能相加当作自然日总数。")
    return "\n\n".join(parts)


def ensure_evidence_sections(answer, agent):
    for step in agent.steps:
        if step.action != "python":
            continue
        try:
            observation = json.loads(step.observation)
        except (ValueError, TypeError):
            continue
        result = observation.get("result")
        if not isinstance(result, dict) or result.get("reconciled") is not True:
            continue
        operation = observation.get("operation")
        if operation in {"attribution", "sequential_attribution"}:
            pairs = result.get("comparisons", []) if operation == "sequential_attribution" else [result]
            for pair in pairs:
                details = pair.get("details", [])[:3]
                required = [pair["baseline_total"], pair["current_total"], pair["total_change"],
                            *(row["change"] for row in details)]
                if covers(answer, required) and all(str(row["dimension"]) in answer for row in details):
                    continue
                base, change = Decimal(pair["baseline_total"]), Decimal(pair["total_change"])
                rate = f"{change / base * 100:.2f}%" if base else "基期为零，未定义"
                lines = ["### 已核对的变化来源", f"{cell(pair['baseline_period'])}：{cell(pair['baseline_total'])}；"
                         f"{cell(pair['current_period'])}：{cell(pair['current_total'])}；"
                         f"净变化：{cell(pair['total_change'])}；变化率：{rate}。",
                         "| 维度 | 变化额 | 占净变化贡献（%） |", "|---|---:|---:|"]
                lines += [f"| {cell(row['dimension'])} | {cell(row['change'])} | {cell(row.get('contribution_percent', '未返回'))} |" for row in details]
                lines.append("仅展示绝对变化最大的前三项。变化贡献不等于因果原因；净变化为零时贡献率未定义。")
                answer += "\n\n" + "\n\n".join(lines[:2]) + "\n\n" + "\n".join(lines[2:])
        elif operation == "driver_check":
            required = [pair[key] for pair in result["comparisons"]
                        for key in ("baseline_orders", "current_orders", "baseline_aov", "current_aov", "order_effect", "aov_effect") if key in pair]
            has_orders = not result.get("order_metric_available") or ("订单" in answer and "客单价" in answer and covers(answer, required))
            has_supply = not any(row.get("baseline_supply") or row.get("current_supply") for pair in result["comparisons"] for row in pair.get("details", [])) or bool(re.search(r"(?:缺货记录|供货记录|供货状态)", answer))
            if not has_orders or not has_supply:
                answer += "\n\n" + driver_evidence(result)
    return answer



def outlier_report(row: dict, metric_label: str) -> str | None:
    """Present the fixed aggregate without asking a model to reinterpret sample counts."""
    if "valid_count" not in row:
        return None
    count = int(row["valid_count"])
    if count == 0:
        return "所选范围没有有效的非空数值，无法计算合计、分位边界或异常值处理结果；这不等于金额为零。"

    def number(key):
        value = row.get(key)
        return format(Decimal(str(value)).normalize(), "f") if value is not None else "未定义"

    retained = int(row["retained_count"])
    return (
        f"{metric_label}：原始合计 **{number('original_amount')}**，缩尾后合计 **{number('winsorized_amount')}**。\n\n"
        f"按 1%／99% 分位边界 **{number('lower_bound')}／{number('upper_bound')}** 处理；"
        f"有效样本 **{count}** 条，空值不参与计算。\n\n"
        f"缩尾保留全部 **{count}** 条有效样本，仅把区间外的数值拉到边界。"
        f"低于下界 **{int(row['below_count'])}** 条，高于上界 **{int(row['above_count'])}** 条。\n\n"
        f"截尾对照：区间内保留 **{retained}** 条，合计 **{number('truncated_amount')}**。"
        + ("零合计是因为全部有效样本被排除，不代表原始金额为零。" if retained == 0 else "")
        + "\n\n处理仅作用于本轮计算视图，未修改原表；样本少时分位边界不宜用于推断一般分布。"
    )
