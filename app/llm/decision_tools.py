"""Function-calling tool schemas and decision mapping for the agent loop.

The model picks exactly one tool per decision round. ``decide_next_step``
mirrors the legacy JSON decision protocol (action + plan + tool inputs), so
the LangGraph loop consumes the same ``AgentDecision`` as before; the report
round is forced to ``finish_report``. On the official DeepSeek endpoint the
schemas are sent in strict mode (server-side schema validation).
"""
import json

NULL_OR = lambda type_schema: {"anyOf": [type_schema, {"type": "null"}]}  # noqa: E731

DATE_RANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                  "description": "起始日期（含），格式 YYYY-MM-DD"},
        "end": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "结束日期（不含），左闭右开，格式 YYYY-MM-DD"},
    },
    "required": ["start", "end"],
    "additionalProperties": False,
}

FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "column": {"type": "string", "description": "筛选字段名（来自表结构）"},
        "values": {
            "type": "array",
            "description": "筛选值列表",
            "items": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]},
        },
    },
    "required": ["column", "values"],
    "additionalProperties": False,
}

ANALYSIS_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["lookup", "period_change", "segment_comparison", "trend",
                                            "ranking", "distribution", "decile", "correlation", "outlier", "summary"],
                 "description": "trend=按粒度的完整时间序列；period_change=两个明确时期；segment_comparison=两个明确群体；lookup=单值查数；ranking=按指标排名的前 N 项；distribution=各维度占比构成；decile=按数值字段分十层的分层分析；correlation=两个数值字段的相关性；outlier=异常值截尾/缩尾处理对比；summary=描述性统计（合计/平均/最小/最大）"},
        "source_table": {"type": "string", "description": "public.开头的实际表名，来自可访问表结构"},
        "metric_label": {"type": "string", "description": "用户可读的指标名称，如：销售额"},
        "metric_expression": {"type": "string",
                              "description": "可加总的数值字段或加减乘表达式，如 sales_amount 或 quantity * unit_price；不含 SUM，不支持比率和均值"},
        "time_column": NULL_OR({"type": "string", "description": "日期字段名"}),
        "time_grain": NULL_OR({"type": "string", "enum": ["day", "week", "month", "quarter", "year"],
                              "description": "按要求按日/周/月/季度/年分组时的粒度；范围合计填 null"}),
        "baseline": NULL_OR(DATE_RANGE_SCHEMA),
        "current": NULL_OR(DATE_RANGE_SCHEMA),
        "group_column": NULL_OR({"type": "string", "description": "群体比较所用的字段名，如：销售大区"}),
        "baseline_group": NULL_OR({"type": "string", "description": "比较的第一个群体标签"}),
        "current_group": NULL_OR({"type": "string", "description": "比较的第二个群体标签"}),
        "dimension": NULL_OR({"type": "string",
                              "description": "用于拆解的真实分类字段名；总体趋势或仅两期总额比较填 null，明确要求拆解时必须填写"}),
        "confirmed_order_column": NULL_OR({"type": "string",
                                          "description": "已确认可加总的订单数统计字段；不确定填 null，禁止猜测"}),
        "limit_n": NULL_OR({"type": "integer", "minimum": 1, "maximum": 50,
                            "description": "ranking 时保留前 N 项（如问前 5 填 5）；其他分析类型填 null"}),
        "rank_column": NULL_OR({"type": "string",
                                "description": "decile 时用于分层的数值字段（按该字段分十层）；其他分析类型填 null"}),
        "correlate_column": NULL_OR({"type": "string",
                                     "description": "correlation 时与主指标计算相关性的第二个数值字段；其他分析类型填 null"}),
        "outlier_mode": NULL_OR({"type": "string", "enum": ["winsorize", "truncate"],
                                 "description": "outlier 时的处理方式：缩尾 winsorize（压到边界）或截尾 truncate（剔除边界外记录）；其他分析类型填 null"}),
        "filters": {"type": "array", "description": "需要保留或新增的筛选条件；无筛选填 []。不得包含 group_column 对应的群体字段（群体比较用 group_column、baseline_group、current_group 指定，不能同时写成筛选）",
                    "items": FILTER_SCHEMA},
        "drill_path": {"type": "array", "description": "已经下钻过的维度序列；未下钻填 []",
                       "items": {"type": "string"}},
    },
    "required": ["kind", "source_table", "metric_label", "metric_expression", "time_column",
                 "time_grain", "baseline", "current", "group_column", "baseline_group",
                 "current_group", "dimension", "confirmed_order_column", "limit_n", "rank_column",
                 "correlate_column", "outlier_mode", "filters", "drill_path"],
    "additionalProperties": False,
}

ANALYSIS_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "question_type": {"type": "string", "enum": ["concept", "lookup", "comparison", "diagnosis"],
                          "description": "问题类型：概念解释 concept、单值查数 lookup、对比 comparison、原因诊断 diagnosis"},
        "requires_sql": {"type": "boolean",
                         "description": "涉及表内具体数值、趋势、排名或原因时必须为 true；仅概念解释时为 false"},
        "metric": {"type": "string", "description": "指标及其计算口径；不适用写 不适用"},
        "time_range": {"type": "string", "description": "时间范围；未指定写 未指定"},
        "comparison": {"type": "string", "description": "对比基准；不适用写 不适用"},
        "dimensions": {"type": "array", "description": "需要拆解的维度列表；没有填 []",
                       "items": {"type": "string"}},
        "analysis_steps": {"type": "array", "description": "按顺序执行的分析步骤",
                           "items": {"type": "string"}},
        "python_operation": NULL_OR({"type": "string",
                                     "enum": ["summary", "percent_change", "share", "profile", "attribution"],
                                     "description": "需要程序计算的算子；不需要填 null"}),
        "chart_type": NULL_OR({"type": "string", "enum": ["line", "bar"],
                               "description": "用户明确要求画图时：趋势用 line、类别比较用 bar；否则 null"}),
        "analysis_context": NULL_OR(ANALYSIS_CONTEXT_SCHEMA),
    },
    "required": ["question_type", "requires_sql", "metric", "time_range", "comparison",
                 "dimensions", "analysis_steps", "python_operation", "chart_type", "analysis_context"],
    "additionalProperties": False,
}

PYTHON_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["summary", "percent_change", "share", "profile", "attribution"],
                      "description": "summary=数量/总和/均值/中位数/最小/最大；percent_change=相邻行变化率（SQL 需先 ORDER BY 且带标签列）；share=各行占比；profile=检查查询结果的行数/缺失/空串/重复；attribution=两个对比组的维度变化归因"},
        "source_round": {"type": "integer", "minimum": 1,
                         "description": "使用哪一轮成功 SQL 的结果（attribution 填维度明细的 SQL 轮次）"},
        "value_column": NULL_OR({"type": "string", "description": "数值列名；profile 填 null"}),
        "label_column": NULL_OR({"type": "string", "description": "标签列名；没有填 null"}),
        "total_source_round": NULL_OR({"type": "integer", "minimum": 1,
                                       "description": "attribution：总额 SQL 的轮次；其他算子填 null"}),
        "period_column": NULL_OR({"type": "string", "description": "attribution：两条 SQL 相同的时期/群体列名；其他填 null"}),
        "dimension_column": NULL_OR({"type": "string", "description": "attribution：明细的维度列名；其他填 null"}),
        "baseline_period": NULL_OR({"type": "string", "description": "attribution：基期（或第一个群体）标签；其他填 null"}),
        "current_period": NULL_OR({"type": "string", "description": "attribution：本期（或第二个群体）标签；其他填 null"}),
    },
    "required": ["operation", "source_round", "value_column", "label_column", "total_source_round",
                 "period_column", "dimension_column", "baseline_period", "current_period"],
    "additionalProperties": False,
}

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "chart_type": {"type": "string", "enum": ["line", "bar"],
                       "description": "趋势用 line，类别比较用 bar"},
        "source_round": {"type": "integer", "minimum": 1,
                         "description": "使用哪一轮成功 SQL 的结果（最多 30 行未截断）"},
        "x_column": {"type": "string", "description": "横轴列名"},
        "y_column": {"type": "string", "description": "数值列名"},
        "title": {"type": "string", "description": "简短图题"},
    },
    "required": ["chart_type", "source_round", "x_column", "y_column", "title"],
    "additionalProperties": False,
}

DECIDE_NEXT_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["sql", "python", "chart", "finish"],
                   "description": "下一步动作：执行查询 sql、程序计算 python、绘图 chart、完成回答 finish"},
        "reasoning": {"type": "string", "description": "本轮决定的简短原因"},
        "plan": NULL_OR(ANALYSIS_PLAN_SCHEMA),
        "sql": NULL_OR({"type": "string", "description": "一条只读 PostgreSQL SELECT；action 非 sql 填 null"}),
        "python": NULL_OR(PYTHON_SCHEMA),
        "chart": NULL_OR(CHART_SCHEMA),
        "content": {"type": "string", "description": "action=finish 时填写面向用户的中文答案；否则简要说明目的"},
    },
    "required": ["action", "reasoning", "plan", "sql", "python", "chart", "content"],
    "additionalProperties": False,
}

FINISH_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "面向用户的中文 Markdown 报告正文"},
    },
    "required": ["content"],
    "additionalProperties": False,
}


def _tool(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": parameters}}


def decision_tools(*, strict: bool, report: bool) -> list[dict]:
    """Tools for one decision round; report rounds may only finish."""
    if report:
        function = _tool("finish_report", "提交最终报告正文，仅输出面向用户的中文 Markdown 回答。",
                         FINISH_REPORT_SCHEMA)
        if strict:
            function["function"]["strict"] = True
        return [function]
    function = _tool(
        "decide_next_step",
        "提交本轮决策：选择一个动作（sql/python/chart/finish）及其结构化参数。"
        "首轮必须携带完整 plan；趋势/比较/归因等任务必须在 plan 内提交完整 analysis_context。",
        DECIDE_NEXT_STEP_SCHEMA)
    if strict:
        function["function"]["strict"] = True
    return [function]


def parse_tool_decision(message) -> dict | None:
    """Extract a decision dict from an OpenAI message's tool_calls."""
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        return None
    if len(tool_calls) > 1:
        return None  # Protocol is one action per round; ask the model to retry.
    call = tool_calls[0]
    name = getattr(call.function, "name", None)
    if name not in {"decide_next_step", "finish_report"}:
        return None
    try:
        arguments = json.loads(call.function.arguments)
    except Exception:
        return None
    if not isinstance(arguments, dict):
        return None
    if name == "finish_report":
        return {"action": "finish", "reasoning": "证据核验后报告", "plan": None,
                "sql": None, "python": None, "chart": None,
                "content": arguments.get("content", "")}
    plan = arguments.get("plan")
    if isinstance(plan, dict):
        if isinstance(plan.get("python_operation"), str) and plan["python_operation"] not in {
                "summary", "percent_change", "share", "profile", "attribution", "decile"}:
            plan["python_operation"] = None
        if isinstance(plan.get("chart_type"), str) and plan["chart_type"] not in {"line", "bar", "scatter"}:
            plan["chart_type"] = None
    return arguments
