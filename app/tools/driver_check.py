"""Exact, bounded supporting checks; mechanical decomposition is not causality."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def number(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("辅助核验数值为空或不是数值，不能按零处理。") from exc
    if not amount.is_finite():
        raise ValueError("辅助核验数值不是有限值。")
    return amount


def rounded(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def rate(before, after):
    return rounded((after - before) / before * 100) if before else None


def compare(before, after, with_orders):
    base, current = number(before["amount"]), number(after["amount"])
    result = {"baseline_amount": str(base), "current_amount": str(current),
              "sales_change": str(current - base), "sales_change_percent": rate(base, current)}
    if not with_orders:
        return result
    qb, qc = number(before["orders"]), number(after["orders"])
    result.update(baseline_orders=str(qb), current_orders=str(qc), orders_change=str(qc - qb),
                  orders_change_percent=rate(qb, qc), baseline_aov=None, current_aov=None,
                  aov_change=None, order_effect=None, aov_effect=None, effects_reconciled=False)
    if qb <= 0 or qc <= 0 or base < 0 or current < 0:
        result["limitation"] = "订单量为零或销售额为负，客单价机械分解不可用；不将未定义客单价填零。"
        return result
    pb, pc = base / qb, current / qc
    # Symmetric two-factor allocation shares the cross-term equally.
    order_effect = (qc - qb) * (pb + pc) / 2
    aov_effect = (pc - pb) * (qb + qc) / 2
    residual = current - base - order_effect - aov_effect
    if abs(residual) > Decimal("0.000001"):
        raise ValueError("订单量与客单价效应未核对回销售额变化，不能报告分解。")
    result.update(baseline_aov=rounded(pb), current_aov=rounded(pc), aov_change=rounded(pc - pb),
                  aov_change_percent=rate(pb, pc), order_effect=rounded(order_effect),
                  aov_effect=rounded(aov_effect), rounding_residual=rounded(current - base - Decimal(rounded(order_effect)) - Decimal(rounded(aov_effect))),
                  effects_reconciled=True)
    return result


def driver_check(rows, totals, *, with_orders, period_order=None):
    if not 2 <= len(totals) <= 30 or not 1 <= len(rows) <= 100:
        raise ValueError("辅助核验需要 2 至 30 个时期、最多 100 行完整辅助记录。")
    periods = [str(row.get("period")) for row in totals]
    if len(set(periods)) != len(periods) or any(not p or p == "None" for p in periods):
        raise ValueError("独立总额必须每期恰好一行，且时期不能为空。")
    expected = {str(row["period"]): number(row.get("amount")) for row in totals}
    if period_order is not None:
        if len(period_order) != len(periods) or set(period_order) != set(periods):
            raise ValueError("辅助核验的比较方向与独立总额时期不一致。")
        periods = period_order
    grouped, sums = {}, {p: Decimal(0) for p in periods}
    order_sums = {p: Decimal(0) for p in periods}
    for row in rows:
        period, dimension = str(row.get("period")), row.get("dimension")
        if period not in expected or dimension is None or not str(dimension).strip():
            raise ValueError("辅助记录时期/维度与当前范围不一致。")
        key = (period, str(dimension))
        if key in grouped:
            raise ValueError("辅助记录包含重复的时期与维度组合。")
        if number(row.get("missing_amount")) != 0:
            raise ValueError("辅助记录包含空销售额，不能忽略后计算客单价。")
        grouped[key] = row
        sums[period] += number(row.get("amount"))
        if with_orders:
            if number(row.get("invalid_orders")) != 0:
                raise ValueError("辅助记录包含空值或负订单量，不能忽略后计算客单价。")
            orders = number(row.get("orders"))
            if orders < 0 or orders != orders.to_integral_value():
                raise ValueError("订单量必须为非负整数，不能把金额或比例当成订单数。")
            order_sums[period] += orders
    if {p for p, _ in grouped} != set(periods) or sums != expected:
        raise ValueError("辅助销售额与独立总额不一致，不能继续解释变化原因。")
    summaries = {p: {"amount": str(sums[p]), "orders": str(order_sums[p])} for p in periods}
    dimensions = sorted({d for _, d in grouped})
    comparisons = []
    for baseline, current in zip(periods, periods[1:]):
        pair = {"baseline_period": baseline, "current_period": current,
                **compare(summaries[baseline], summaries[current], with_orders), "details": []}
        for dimension in dimensions:
            before, after = grouped.get((baseline, dimension)), grouped.get((current, dimension))
            if before is None or after is None:
                pair["details"].append({"dimension": dimension, "limitation": "某个比较时期未返回该维度，未补零计算客单价或原因效应。"})
                continue
            pair["details"].append({"dimension": dimension, **compare(before, after, with_orders),
                                    "baseline_supply": supply(before), "current_supply": supply(after)})
        pair["details"].sort(key=lambda d: abs(number(d.get("sales_change", 0))), reverse=True)
        comparisons.append(pair)
    return {"reconciled": True, "comparisons": comparisons,
            "order_metric_available": with_orders,
            "method": "销售额=订单量×客单价；客单价=总销售额/总订单量。对称机械分解：订单量效应=订单量变化×两期平均客单价；客单价效应=客单价变化×两期平均订单量。两项核对净变化，显示值舍入差额单独列出。",
            "caution": "这是同口径的机械分解，不是因果证明。客单价包含商品/订单结构影响，不等于商品售价；供货状态与记录缺货天数只能作为关联线索，记录区间不是实际自然日总数。"}


def supply(row):
    return {key: row[key] for key in ("stockout_min", "stockout_max", "stockout_distinct", "supply_statuses") if key in row}
