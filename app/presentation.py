"""Pure conversation formatting helpers for command-line and regression checks."""

import re
from datetime import datetime, timedelta, timezone


_CHART_PATH = re.compile(r"/api/v1/charts/[A-Za-z0-9_-]+\.png")
_SHANGHAI = timezone(timedelta(hours=8))


def display_time(value: str) -> str:
    created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return created.astimezone(_SHANGHAI).strftime("%m-%d %H:%M")


def response_charts(response: dict) -> list[str]:
    charts = response.get("charts") or []
    single = response.get("chart_url")
    return list(dict.fromkeys([*charts, *([single] if single else [])]))


def history_messages(turns: list[dict]) -> list[dict]:
    messages = []
    for turn in turns:
        messages.append({"role": "user", "content": turn["question"], "charts": []})
        answer = turn.get("answer") or f"本轮状态：{turn['status']}"
        messages.append({
            "role": "assistant",
            "content": answer,
            "charts": list(dict.fromkeys([*(turn.get("charts") or []), *_CHART_PATH.findall(answer)])),
            "suggestions": turn.get("suggestions") or [],
            "analysis_context": turn.get("analysis_context"),
        })
    return messages


def latest_history_context(turns: list[dict]) -> dict | None:
    for turn in reversed(turns):
        if turn.get("context_reset"):
            return None
        if turn.get("session_context"):
            return turn["session_context"]
        if turn.get("analysis_context"):
            return turn["analysis_context"]
    return None


def context_caption(context: dict) -> str:
    def period(value: dict | None) -> str:
        return f"{value['start']} 至 {value['end']}（不含结束日）" if value else "未限定时间"
    if context["kind"] == "period_change":
        comparison = f"{period(context.get('baseline'))} → {period(context.get('current'))}"
    elif context["kind"] == "segment_comparison":
        comparison = f"{context['baseline_group']} → {context['current_group']} · {period(context.get('current'))}"
    else:
        comparison = period(context.get("current"))
    labels = {"channel": "渠道", "category": "品类", "region": "地区"}
    filters = "；".join(
        f"{labels.get(item['column'], item['column'])}：{'、'.join(map(str, item['values']))}"
        for item in context.get("filters", [])
    )
    return f"{context['metric_label']} · {comparison}" + (f" · 筛选 {filters}" if filters else " · 全部数据")
