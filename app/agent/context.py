"""Persisted analysis scope; old answers are never reused as numeric evidence."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DateRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def chronological(self) -> "DateRange":
        if self.start >= self.end:
            raise ValueError("日期范围的结束日期必须晚于开始日期（结束日期不包含在内）。")
        return self

    def label(self) -> str:
        return f"{self.start.isoformat()} 至 {self.end.isoformat()}（不含）"


class ScopeFilter(BaseModel):
    column: str = Field(min_length=1, max_length=128)
    values: list[str | int | float | bool] = Field(min_length=1, max_length=20)


class AnalysisContext(BaseModel):
    kind: Literal["lookup", "period_change", "segment_comparison", "trend"]
    source_table: str = Field(min_length=1, max_length=160)
    metric_label: str = Field(min_length=1, max_length=80)
    metric_expression: str = Field(min_length=1, max_length=300)
    time_column: str | None = None
    time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    baseline: DateRange | None = None
    current: DateRange | None = None
    group_column: str | None = None
    baseline_group: str | None = None
    current_group: str | None = None
    dimension: str | None = None
    confirmed_order_column: str | None = None
    filters: list[ScopeFilter] = Field(default_factory=list, max_length=10)
    drill_path: list[str] = Field(default_factory=list, max_length=20)


class FollowupSuggestion(BaseModel):
    label: str
    question: str


def is_followup(question: str, context: AnalysisContext | None) -> bool:
    if context is None:
        return False
    if re.search(r"新问题|换个问题|重新开始|另一张表|换表", question):
        return False
    tables = re.findall(r"(?:public\.)?(?:user_[\w]+|demo_[\w]+|agent_[\w]+)", question)
    if tables and any(table.removeprefix("public.") != context.source_table.removeprefix("public.")
                      for table in tables):
        return False
    return bool(re.search(
        r"继续|下钻|再按|再看|刚才|上述|其中|那|换成|改成|改为|同比|环比|沿用|按.*(?:拆解|归因|看)",
        question,
    ))


def merge_context(proposal: AnalysisContext, previous: AnalysisContext | None,
                  question: str) -> AnalysisContext:
    """Keep filters on a follow-up unless the user explicitly asks to remove them."""
    if previous is not None:
        # 模型提议的表与已选表不同：只有用户明确提到新表才换，否则沿用已选表。
        proposal_table = proposal.source_table.removeprefix("public.")
        previous_table = previous.source_table.removeprefix("public.")
        if proposal_table != previous_table and proposal_table not in question:
            proposal = proposal.model_copy(deep=True)
            proposal.source_table = previous.source_table
    if not is_followup(question, previous):
        return proposal.model_copy(update={"confirmed_order_column": None})
    result = proposal.model_copy(deep=True)
    result.source_table = previous.source_table
    result.confirmed_order_column = previous.confirmed_order_column
    if not re.search(r"销售|订单|金额|利润|收入|客单|数量|SUM|sum", question):
        result.metric_expression = previous.metric_expression
        result.metric_label = previous.metric_label
    if not re.search(r"\d.*(?:年|月|日)|\d{4}-\d{2}-\d{2}|同比|环比|上月|去年|改.*(?:时期|日期范围)|换.*(?:时期|日期范围)", question):
        result.time_column = previous.time_column
        if result.kind in {"trend", "lookup"} and previous.kind == "period_change":
            result.baseline = None
            if result.kind == "lookup" and re.search(r"当期|本期|这个月", question):
                result.current = previous.current
            elif result.kind == "lookup" and re.search(r"基期|上期", question):
                result.current = previous.baseline
            else:
                result.current = DateRange(
                    start=min(previous.baseline.start, previous.current.start),
                    end=max(previous.baseline.end, previous.current.end),
                )
        elif result.kind == "period_change" and previous.kind != "period_change":
            pass  # The new comparison must supply its two ranges; a trend has no baseline.
        else:
            result.baseline = previous.baseline
            result.current = previous.current
    if not re.search(r"取消|去掉|移除|不限制|全部渠道|所有渠道|全部品类|所有品类|回到总体", question):
        merged = {item.column: item for item in previous.filters}
        if not (re.search(r"沿用.*筛选", question) and not re.search(r"新增|限定|只看|改为|换成", question)):
            merged.update({item.column: item for item in proposal.filters})
        result.filters = list(merged.values())
    result.drill_path = list(dict.fromkeys([
        *previous.drill_path,
        *([previous.dimension] if previous.dimension != result.dimension and previous.dimension else []),
        *proposal.drill_path,
    ]))
    if "回到总体" in question:
        result.filters = []
        result.drill_path = []
    return result
