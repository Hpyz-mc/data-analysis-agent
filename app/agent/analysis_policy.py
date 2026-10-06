"""Structured analysis tasks and conservative routing for explicit comparisons."""

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.intent import requested_time_grain


ANALYSIS_CLARIFICATION_PREFIX = "为了准确分析，请先明确比较基准："

AnalysisKind = Literal[
    "lookup", "trend", "period_change", "segment_comparison", "clarification",
    "ranking", "distribution", "decile", "correlation", "outlier", "summary",
]


class AnalysisTask(BaseModel):
    kind: AnalysisKind = "lookup"
    axis: Literal["none", "time", "segment"] = "none"
    metric: str | None = None
    comparison: str | None = None
    time_range: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    clarification: str | None = None


def build_analysis_task(question: str) -> AnalysisTask:
    """Choose an evidence contract; do not guess a missing comparison baseline."""
    if re.search(r"什么是|如何理解|定义|解释.*概念", question):
        return AnalysisTask()

    # A series with a breakdown is not a single, unspecified two-period diagnosis.
    # Also tolerate the omitted first character in “析销售额的月度变化”.
    if requested_time_grain(question):
        return AnalysisTask(kind="trend", axis="time")
    # “趋势/走势”是时间序列，优先于“为什么变化”诊断；“变化来源/拆解”不是孤立的诊断。
    if re.search(r"走势|趋势", question) and not re.search(r"为什么|为何|原因|归因", question):
        return AnalysisTask(kind="trend", axis="time")
    # 显式要求截尾/缩尾处理优先于“异常”诊断。
    if re.search(r"截尾|缩尾|去极值|异常值.{0,8}(处理|检测|清洗)|处理.{0,8}异常值", question):
        return AnalysisTask(kind="outlier", axis="none")

    months = re.findall(r"(?:20\d{2}\s*年\s*)?(?:1[0-2]|[1-9])\s*月", question)
    if len(months) >= 2 and re.search(r"变化额|变化率|相差|差额|比较|对比", question):
        return AnalysisTask(kind="period_change", axis="time")

    diagnostic = bool(re.search(r"为什么|为何|原因|归因|下钻|诊断|异常", question))
    diagnostic |= bool(
        re.search(r"分析|解释|研究|调查", question)
        and re.search(r"下降|下滑|减少|上涨|增长|变化|波动|差异", question)
    )
    if diagnostic:
        months = re.findall(r"(?:20\d{2}\s*年\s*)?(?:1[0-2]|[1-9])\s*月", question)
        quarters = re.findall(r"(?:第?[一二三四1-4]\s*季度?|Q[1-4])", question, re.I)
        years = re.findall(r"20\d{2}\s*年", question)
        time_pair = len(months) >= 2 or len(quarters) >= 2 or len(years) >= 2 or bool(
            re.search(r"环比|同比|上月|前月|去年同期|上期|前一期", question)
        )
        if time_pair:
            return AnalysisTask(kind="period_change", axis="time")

        segment = bool(re.search(r"地区|区域|渠道|品类|类别|部门|客户群|人群|产品|商品", question))
        # “比较月份/沿用比较时期” is a time reference, not two segments.
        comparison_text = re.sub(r"比较(?:月份|时期|时间|范围)|对比(?:月份|时期)", "", question)
        segment_pair = segment and bool(re.search(
            r"与|对比|相比|差异|不同|高于|低于|(?<![较环同])比(?!较|例|率)", comparison_text,
        ))
        if segment_pair:
            return AnalysisTask(kind="segment_comparison", axis="segment")

        if re.search(r"下降|下滑|减少|上涨|增长|变化|波动", question):
            clarification = (
                f"{ANALYSIS_CLARIFICATION_PREFIX}你希望与哪个时期比较？"
                "例如上个月、去年同期，或指定两个日期范围。"
            )
        else:
            clarification = (
                f"{ANALYSIS_CLARIFICATION_PREFIX}请指定要比较的两个群体或时期，"
                "以及希望分析的指标。"
            )
        return AnalysisTask(kind="clarification", clarification=clarification)

    if re.search(r"分析|解释|研究|调查|查看|看", question) and re.search(
        r"走势|趋势|逐月|逐年", question,
    ):
        return AnalysisTask(kind="trend", axis="time")
    if re.search(r"前\s*\d+(?:\s*[名个项位条家])?|TOP\s*\d+|排名|排行", question, re.I):
        return AnalysisTask(kind="ranking", axis="segment")
    if re.search(r"十分位|十等分|十档|十分档|分位数|分位|分层|分箱", question):
        return AnalysisTask(kind="decile", axis="segment")
    if re.search(r"占比|比例|构成|份额|百分比", question):
        return AnalysisTask(kind="distribution", axis="segment")
    if re.search(r"平均值|均值|平均(?:订单金额|销售额)|中位数|最大(?:值|金额)|最小(?:值|金额)", question):
        return AnalysisTask(kind="summary", axis="none")
    if re.search(r"相关性|相关系数|是否相关|相关吗|相关不|关联性|关联度|pearson|皮尔逊", question, re.I):
        return AnalysisTask(kind="correlation", axis="none")
    return AnalysisTask()
