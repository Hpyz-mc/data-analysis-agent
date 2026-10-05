"""Explicit reporting boundaries derived from this turn, never historic numbers."""
import json
import re
from decimal import Decimal, InvalidOperation


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _month_index(label):
    year, month = map(int, label.split("-"))
    return year * 12 + month - 1


def report_facts(state):
    facts = {
        "scope_rule": "未查询不等于无记录。查询范围外的日期、群体或维度不得判为缺失、零值或无数据。",
        "precision_rule": "数字优先引用工具或下列精确计算；不要用模糊上限替代精确差额。",
    }
    # The active plan is this turn's scope; persisted context may describe an older comparison.
    context = state.plan.analysis_context if state.plan else None
    if context:
        ranges = []
        for label, period in (("基期", context.baseline), ("当期/查询范围", context.current)):
            if period:
                ranges.append({"label": label, "start_inclusive": period.start.isoformat(),
                               "end_exclusive": period.end.isoformat(),
                               "note": "结束日期及之后不在该区间内；不出现对应记录不能解释为该日期无数据。"})
        facts["query_ranges"] = ranges
        facts["filters"] = [item.model_dump() for item in context.filters]
        if context.kind == "period_change" and not context.dimension:
            baseline, current = context.baseline, context.current
            def full_month(period):
                return (period.start.day == period.end.day == 1
                        and period.end.year * 12 + period.end.month
                        - period.start.year * 12 - period.start.month == 1)
            month_interval = (current.start.year * 12 + current.start.month
                              - baseline.start.year * 12 - baseline.start.month)
            facts["period_comparison"] = {
                "baseline_period": baseline.label(), "current_period": current.label(),
                "month_interval": month_interval,
                "is_month_over_month": full_month(baseline) and full_month(current) and month_interval == 1,
                "note": "按明确的基期→当期解释差额。只有相邻完整自然月可称月环比；不要把间隔月份填零或额外查询。",
            }

    monthly, contributions = [], []
    for step in state.steps:
        try:
            observation = json.loads(step.observation)
        except (ValueError, TypeError):
            continue
        if not isinstance(observation, dict):
            continue
        if step.action == "sql" and not observation.get("truncated"):
            rows = observation.get("rows")
            if not isinstance(rows, list) or len(rows) < 2:
                continue
            points = []
            for row in rows:
                if not isinstance(row, dict) or len(row) != 2:
                    break  # Never combine dimension rows into a fictitious monthly series.
                period = row.get("period", row.get("month"))
                value = _number(row.get("amount", row.get("sales_amount")))
                if not isinstance(period, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", period) or value is None:
                    break
                points.append((period, value))
            else:
                if len({period for period, _ in points}) != len(points):
                    continue
                points.sort()
                highest = max(value for _, value in points)
                lowest = min(value for _, value in points)
                monthly.append({
                    "source_round": step.round,
                    "returned_months": [period for period, _ in points],
                    "maximum": {"value": str(highest), "months": [p for p, v in points if v == highest]},
                    "minimum": {"value": str(lowest), "months": [p for p, v in points if v == lowest]},
                    "observed_max_minus_min": str(highest - lowest),
                    "adjacent_changes": [{"from": before[0], "to": after[0],
                                          "difference": str(after[1] - before[1]),
                                          "month_interval": _month_index(after[0]) - _month_index(before[0]),
                                          "is_month_over_month": _month_index(after[0]) - _month_index(before[0]) == 1}
                                         for before, after in zip(points, points[1:])],
                    "note": "最高/最低必须引用对应月份（含并列）。间隔不是一个月的比较不得称月环比；不能推断未返回月份在原表中没有记录，也不能补零。",
                })
        if step.action == "python" and observation.get("operation") in {"attribution", "sequential_attribution"}:
            result = observation.get("result")
            if not isinstance(result, dict) or result.get("reconciled") is not True:
                continue
            comparisons = result.get("comparisons", []) if observation["operation"] == "sequential_attribution" else [result]
            for comparison in comparisons:
                details = comparison.get("details", [])
                rates = []
                for detail in details:
                    base, current = _number(detail.get("baseline_value")), _number(detail.get("current_value"))
                    if base and current is not None:
                        rates.append((current - base) / base * 100)
                same_rate = str(rates[0]) if len(rates) == len(details) and rates and all(r == rates[0] for r in rates) else None
                positive = sum((_number(d.get("change")) or Decimal(0) for d in details if (_number(d.get("change")) or 0) > 0), Decimal(0))
                negative = sum((_number(d.get("change")) or Decimal(0) for d in details if (_number(d.get("change")) or 0) < 0), Decimal(0))
                contributions.append({
                    "source_round": step.round, "baseline_period": comparison.get("baseline_period"),
                    "current_period": comparison.get("current_period"), "denominator": comparison.get("total_change"),
                    "definition": "本轮筛选范围内的维度变化额 / 本轮净变化额 × 100%；净变化为零时贡献率未定义。",
                    "interpretation": "正负方向按净变化分母解释，可能超过 100%；不等于该维度自身变化率，也不证明因果。",
                    "scope": "无筛选的总体归因可称总体维度贡献；限定渠道等筛选后的贡献只属于该子范围，不能当总体贡献。",
                    "positive_offset": str(positive), "negative_drag": str(negative),
                    "uniform_own_change_percent": same_rate,
                    "uniform_rate_interpretation": "若各维度自身变化率相同，贡献最大的维度主要因为基期规模较大，不能说它独有异常；贡献集中不等于降幅更异常。",
                })
    facts["monthly_evidence"] = monthly
    facts["contribution_semantics"] = contributions
    return json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
