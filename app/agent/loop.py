import json
import re
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any, Literal, TypedDict
from types import SimpleNamespace

from langgraph.graph import END, START, StateGraph

from app.agent.analysis_policy import ANALYSIS_CLARIFICATION_PREFIX, build_analysis_task
from app.agent.semantics import semantic_sql_error
from app.agent.context import AnalysisContext, is_followup, merge_context
from app.agent.intent import grain_feedback
from app.agent.diagnostics import AnalysisDiagnostics, tool_receipts
from app.agent.report_fallback import partial_report
from app.agent.report_presentation import ensure_evidence_sections
from app.agent.answer_contract import build_contract, append_driver_pipeline, append_requested_charts
from app.agent.scoped_analysis import (
    ScopeError, align_time_intent, align_breakdown_intent, build_pipeline, followup_suggestions,
    missing_requested_dimension, requested_breakdown_labels, validate_context,
)
from app.agent.state import AgentState, AgentStep, AnalysisPlan, ConversationTurn
from app.llm.deepseek import DeepSeekClient, LLMError
from app.schemas.analysis import AnalysisResponse
from app.tools.chart_tool import ChartRequest, ChartTool, ChartToolError
from app.tools.python_tool import PythonAnalysisRequest, PythonAnalysisTool, PythonToolError
from app.tools.sql_tool import SQLTool, SQLToolError


TRUNCATED_RESULT_MESSAGE = "查询数据量过大，请缩小查询范围（如按月份、按类别聚合）"


_DATA_INTENT = re.compile(
    r"查询|查|分析|看|销售|订单|金额|数据|趋势|走势|销量|利润|客单价|数量|单价|折扣"
    r"|品类|渠道|地区|客户|统计|汇总|合计|同比|环比|增长|下降|变化|报表"
)
_NON_ANALYSIS = re.compile(r"泄露|密钥|密码|令牌|忽略.*提示|系统提示", re.I)

# 当前演示数据没有的维度：出现即澄清一次，避免模型为不存在的主题空转轮次。
_AVAILABLE_HINT = ("现有数据可分析：销售额、订单量、渠道、品类、日期趋势、排名、占比、分层、相关性等。")
_SUBJECT_GAP = [
    (re.compile(r"用户(?!补充)|注册|会员|复购|留存|活跃|新增用户|用户增长"),
     "当前可访问的数据表没有用户/注册/会员相关字段，无法完成这类分析。" + _AVAILABLE_HINT),
    (re.compile(r"流量|点击|访问|浏览|曝光|广告|转化"),
     "当前可访问的数据表没有流量/广告/访问相关字段，无法完成这类分析。" + _AVAILABLE_HINT),
    (re.compile(r"库存|缺货率|备货|仓储"),
     "当前可访问的数据表没有库存/仓储相关字段，无法完成这类分析。" + _AVAILABLE_HINT),
    (re.compile(r"退款|退货|售后|投诉"),
     "当前可访问的数据表没有退款/售后相关字段，无法完成这类分析。" + _AVAILABLE_HINT),
    (re.compile(r"评论|评分|评价"),
     "当前可访问的数据表没有评论/评分相关字段，无法完成这类分析。" + _AVAILABLE_HINT),
    (re.compile(r"物流|配送|发货|签收"),
     "当前可访问的数据表没有物流/配送相关字段，无法完成这类分析。" + _AVAILABLE_HINT),
    (re.compile(r"system_upload_records|analysis_sessions|analysis_turns|内部管理表|管理表"),
     "平台内部管理表（会话、上传记录等）不属于业务数据，禁止查询。" + _AVAILABLE_HINT),
]


def _subject_gap(question: str) -> str | None:
    """当前可访问表中不存在的业务维度或禁止查询的内部表；返回澄清文案或 None。"""
    for pattern, message in _SUBJECT_GAP:
        if pattern.search(question):
            return message
    return None


def _is_data_question(question: str) -> bool:
    """数据查询/分析问题需要表和计划；拒绝、元问题等非分析请求不需要。"""
    return bool(_DATA_INTENT.search(question)) and not _NON_ANALYSIS.search(question)


class WorkflowState(TypedDict, total=False):
    """One request's graph state; PostgreSQL conversation history stays separate."""

    agent: AgentState
    schema: str
    failed_sql: set[str]
    timings_ms: dict[str, int]
    started: float
    decision: Any
    next_action: str
    report_mode: Literal["finish", "truncated", "incomplete", "clarification"]
    response: AnalysisResponse
    clarification: str | None
    round_limit: int
    pipeline: list[dict]
    scoped_analysis: bool
    required_pipeline: list[dict]
    pipeline_start_round: int


class DataAnalysisAgent:
    """Bounded LangGraph workflow for lookups, trends, and change diagnosis."""

    def __init__(
        self,
        llm: DeepSeekClient,
        sql_tool: SQLTool,
        python_tool: PythonAnalysisTool,
        chart_tool: ChartTool,
        max_rounds: int = 7,
    ) -> None:
        self.llm = llm
        self.sql_tool = sql_tool
        self.python_tool = python_tool
        self.chart_tool = chart_tool
        self.max_rounds = max_rounds

    def run(
        self, question: str, conversation_history: list[ConversationTurn] | None = None,
        analysis_context: AnalysisContext | None = None,
        emit=None,
    ) -> AnalysisResponse:
        self._emit = emit
        self._streamed_report = False
        self._trace = AnalysisDiagnostics()
        task_question = question
        if (conversation_history and conversation_history[-1].answer.startswith(
                ("为了准确分析", "我检测到数据库中存在多份相似的数据表"))
                and not re.search(r"新问题|换个问题|重新开始|先只|只分析|仅看", question)
                and not _NON_ANALYSIS.search(question)
                and not re.search(r"查询|分析|走势|趋势|占比|构成|排名|前\s*\d|十分位|分层"
                                  r"|相关|截尾|缩尾|客单价|平均|最高|最低|画|图|哪个|哪些|多少|为什么|为何", question)):
            task_question = f"{conversation_history[-1].question}；用户补充：{question}"
        task = build_analysis_task(task_question)
        if is_followup(question, analysis_context) and task.kind in {"lookup", "clarification"}:
            task.kind = analysis_context.kind
            task.axis = "none" if task.kind == "lookup" else "segment" if task.kind == "segment_comparison" else "time"
            task.clarification = None
        round_limit = self.max_rounds + (
            2 if task.kind in {"period_change", "segment_comparison"} else 0
        )
        graph = self._build_graph()
        initial_state = {
                "agent": AgentState(
                    question=task_question, task=task,
                    conversation_history=conversation_history or [],
                    analysis_context=analysis_context,
                ),
                "round_limit": round_limit,
                "failed_sql": set(),
                "timings_ms": {},
                "started": perf_counter(),
                "pipeline": [], "scoped_analysis": False, "required_pipeline": [],
            }
        try:
            result = graph.invoke(initial_state, config={"recursion_limit": max(50, round_limit * 4 + 10)})
        except Exception as exc:
            self._stop(getattr(exc, "code", "database_error" if isinstance(exc, SQLToolError) else "internal_error"))
            self._trace.status = "failed"
            raise
        response = result["response"]
        snapshot = getattr(self.llm, "usage_snapshot", None)
        if callable(snapshot):
            response.usage = snapshot()
        response.diagnostics = self.diagnostics_snapshot()
        return response

    def diagnostics_snapshot(self):
        return getattr(self, "_trace", AnalysisDiagnostics()).model_copy(deep=True)

    def _track(self, stage, agent=None, route=None):
        self._trace.stage = stage
        if agent is not None:
            self._trace.tools = tool_receipts(agent.steps)
            self._trace.last_round = agent.current_round
        if route is not None:
            self._trace.route = route

    def _stop(self, reason):
        if self._trace.stop_reason is None:
            self._trace.stop_reason = reason
            self._trace.stopped_at = self._trace.stage

    def _reject_initial_plan(self, agent, timings, feedback):
        agent.feedback = feedback
        if self._trace.planning_attempts >= 2:
            self._stop("planning_limit")
            agent.feedback = f"两次规划仍未提供有效分析范围；最近一次原因：{feedback}"
            return {"agent": agent, "timings_ms": timings, "report_mode": "incomplete", "next_action": "report"}
        return {"agent": agent, "timings_ms": timings, "next_action": "decide"}

    def _build_graph(self):
        builder = StateGraph(WorkflowState)
        builder.add_node("load_schema", self._load_schema_node)
        builder.add_node("decide", self._decide_node)
        builder.add_node("sql", self._sql_node)
        builder.add_node("python", self._python_node)
        builder.add_node("chart", self._chart_node)
        builder.add_node("report", self._report_node)
        builder.add_edge(START, "load_schema")
        builder.add_conditional_edges("load_schema", self._route_after_schema, {
            "decide": "decide", "report": "report",
        })
        builder.add_conditional_edges("decide", self._route_after_decision, {
            "sql": "sql", "python": "python", "chart": "chart",
            "decide": "decide", "report": "report",
        })
        for tool in ("sql", "python", "chart"):
            builder.add_conditional_edges(tool, self._route_after_tool, {
                "decide": "decide", "report": "report",
            })
        builder.add_edge("report", END)
        return builder.compile()

    def _load_schema_node(self, state: WorkflowState) -> dict:
        self._track("schema", state["agent"])
        emit = getattr(self, "_emit", None)
        if emit:
            emit("status", {"message": "正在读取数据结构…"})
        schema = self.sql_tool.get_schema()
        check_versions = getattr(self.sql_tool, "find_ambiguous_imports", None)
        question = state["agent"].question
        if is_followup(question, state["agent"].analysis_context):
            question += " " + state["agent"].analysis_context.source_table
        clarification = check_versions(question) if check_versions else None
        if not clarification and is_followup(state["agent"].question, state["agent"].analysis_context):
            missing = missing_requested_dimension(
                state["agent"].question, state["agent"].analysis_context, schema,
            )
            if missing:
                clarification = f"为了准确分析，请先确认分析范围：{missing}"
        if (not clarification
                and not re.search(r"什么是|如何理解|定义|解释.*概念", state["agent"].question)):
            gap = _subject_gap(state["agent"].question)
            if gap:
                clarification = gap
        if (not clarification and state["agent"].analysis_context is None
                and _is_data_question(state["agent"].question)
                and not re.search(r"什么是|如何理解|定义|解释.*概念", state["agent"].question)):
            tables = [line.split(": ", 1)[0].removeprefix("public.")
                      for line in schema.splitlines() if ": " in line]
            if len(tables) > 1 and not any(table in state["agent"].question for table in tables):
                clarification = ("为了准确分析，请先明确要分析的数据表、指标和时间范围。当前可访问的数据表："
                                 + "、".join(tables) + "。")
        clarification = clarification or state["agent"].task.clarification
        if clarification:
            self._stop("scope_clarification")
        timings = dict(state["timings_ms"])
        timings["schema"] = round((perf_counter() - state["started"]) * 1000)
        return {"schema": schema, "timings_ms": timings, "clarification": clarification,
                "report_mode": "clarification" if clarification else "incomplete"}

    def _route_after_schema(self, state: WorkflowState) -> str:
        return "report" if state.get("clarification") else "decide"

    def _decide_node(self, state: WorkflowState) -> dict:
        agent = state["agent"].model_copy(deep=True)
        agent.current_round += 1
        if state.get("pipeline"):
            pipeline = list(state["pipeline"])
            item = pipeline.pop(0)
            decision = self._pipeline_decision(item)
            return {"agent": agent, "decision": decision, "pipeline": pipeline,
                    "next_action": decision.action}
        if state.get("scoped_analysis"):
            feedback = self._pipeline_feedback(state) or self._finish_feedback(agent)
            if feedback:
                # Do not spend model calls asking to finish a failed fixed pipeline.
                agent.feedback = feedback
                return {"agent": agent, "report_mode": "incomplete", "next_action": "report"}
        llm_started = perf_counter()
        emit = getattr(self, "_emit", None)
        report_ready = agent.plan is not None and not self._finish_feedback(agent)
        self._track("report" if report_ready else "planning" if agent.plan is None else "decision", agent)
        if agent.plan is None:
            self._trace.planning_attempts += 1
        try:
            if emit and report_ready:
                emit("status", {"message": "证据核验完成，正在生成回答…"})
                content = self.llm.stream_report(agent, state["round_limit"], state["schema"], emit)
                self._streamed_report = True
                decision = SimpleNamespace(action="finish", content=content, plan=None)
            else:
                if emit:
                    emit("status", {"message": "正在规划分析与检查证据…"})
                kwargs = ({"report": report_ready} if isinstance(self.llm, DeepSeekClient) else {})
                decision = self.llm.decide(agent, state["round_limit"], state["schema"], **kwargs)
        except LLMError as exc:
            self._stop(exc.code)
            if exc.code == "stream_cancelled" or not any(self._has_usable_sql_rows(step) for step in agent.steps if step.action == "sql"):
                raise  # Failures without usable evidence remain genuine errors.
            agent.feedback = (f"查询与计算结果已保留，但文字报告未完成：{exc}" if report_ready else
                              f"分析已停止，已有查询与计算结果保留，尚未完成全部要求。停止原因：{exc}")
            timings = dict(state["timings_ms"])
            timings[f"llm_round_{agent.current_round}"] = round((perf_counter() - llm_started) * 1000)
            if emit:
                emit("status", {"message": "本轮分析未完成，正在保留已有结果…"})
            return {"agent": agent, "timings_ms": timings, "report_mode": "incomplete", "next_action": "report"}
        timings = dict(state["timings_ms"])
        timings[f"llm_round_{agent.current_round}"] = round(
            (perf_counter() - llm_started) * 1000
        )
        if agent.plan is None:
            prepared = self._accept_initial_plan(agent, decision, state, timings)
            if prepared is not None:
                return prepared
        return self._resolve_decision(agent, decision, state, timings)

    def _accept_initial_plan(self, agent, decision, state, timings):
        """Validate the initial scope and prepare tools without executing them."""
        if agent.plan is None:
            if decision.plan is None:
                if decision.action == "finish" and not _is_data_question(agent.question):
                    agent.plan = AnalysisPlan(
                        question_type="concept", requires_sql=False, metric="不适用",
                        time_range="未指定", comparison="不适用", analysis_steps=["回答非分析请求"],
                    )
                    return {"agent": agent, "decision": decision, "timings_ms": timings,
                            "next_action": "report", "report_mode": "finish"}
                return self._reject_initial_plan(agent, timings, "第一轮必须先给出 plan，再决定是否使用工具。")
            structured = agent.task.kind in {"period_change", "segment_comparison", "trend",
                                             "ranking", "distribution", "decile", "correlation", "outlier", "summary"}
            structured |= decision.plan.question_type in {"comparison", "diagnosis"}
            structured |= is_followup(agent.question, agent.analysis_context)
            structured |= bool(re.search(r"客单价|缺货|断货|供货|采购数量|折扣|金额.*数量|销售额.*订单|订单.*销售额", agent.question))
            if not decision.plan.requires_sql and re.search(r"什么是|如何理解|定义|解释.*概念", agent.question):
                structured = False
            # 任何数据问题（含 lookup 自由路径）都不允许概念计划，避免虚构金额。
            if (not decision.plan.requires_sql and _is_data_question(agent.question)
                    and not re.search(r"什么是|如何理解|定义|解释.*概念", agent.question)):
                return self._reject_initial_plan(agent, timings, "这是数据分析任务，必须查询数据库，不能改成概念回答。若当前表确实没有相关字段或数据，请明确说明缺少哪个字段并标注无法查询的原因，不得编造数字。")
            if structured and decision.plan.analysis_context is None:
                return self._reject_initial_plan(agent, timings,
                    "趋势、比较、原因及辅助指标分析必须提供完整 analysis_context：kind、source_table、metric_label、"
                    "metric_expression、time_column、baseline/current、dimension、filters。用 action=finish 提交 plan，"
                    "sql/python/chart=null，由程序执行查询；范围不确定时不能擅自猜测。")
            if (decision.plan.analysis_context is not None and agent.task.kind in {"period_change", "segment_comparison", "trend",
                    "ranking", "distribution", "decile", "correlation", "outlier", "summary"}
                    and decision.plan.analysis_context.kind != agent.task.kind):
                return self._reject_initial_plan(agent, timings, f"本轮分析类型为 {agent.task.kind}，analysis_context.kind 必须一致，不能用单次查数代替比较分析。")
            if not decision.plan.requires_sql:
                agent.task.kind, agent.task.axis, agent.task.clarification = "lookup", "none", None
            agent.plan = decision.plan
            if agent.plan.analysis_context is not None:
                try:
                    context = merge_context(
                        agent.plan.analysis_context, agent.analysis_context, agent.question,
                    )
                    if "." not in context.source_table:
                        context.source_table = "public." + context.source_table
                    context = align_breakdown_intent(context, agent.question, state["schema"])
                    context = align_time_intent(context, agent.question, state["schema"])
                    context = validate_context(context, state["schema"], agent.question)
                    contract = build_contract(agent.question, context, state["schema"])
                    chart_type = (
                        (agent.plan.chart_type if context.dimension or context.time_grain else None)
                        if context.kind == "lookup" else "line" if context.kind == "trend" else "bar"
                    )
                    if context.kind == "period_change" and not context.dimension:
                        chart_type = agent.plan.chart_type if re.search(r"图|画|绘制", agent.question) else None
                    if context.kind == "segment_comparison" and not context.dimension:
                        chart_type = None
                    if context.kind == "correlation":
                        chart_type = "scatter"
                    if context.kind in {"outlier", "summary"}:
                        chart_type = None
                    pipeline = build_pipeline(context, agent.current_round, chart_type,
                                              contribution_chart="贡献图" in agent.question)
                    pipeline = append_requested_charts(pipeline, context, agent.question, agent.current_round)
                    pipeline = append_driver_pipeline(pipeline, context, contract, agent.current_round)
                except ScopeError as exc:
                    self._stop("scope_clarification")
                    return {"agent": agent, "timings_ms": timings, "next_action": "report",
                            "report_mode": "clarification",
                            "clarification": f"为了准确分析，请先确认分析范围：{exc}"}
                agent.analysis_context = context
                agent.plan.analysis_context = context
                agent.task.kind = context.kind
                agent.task.axis = ("none" if context.kind in {"lookup", "correlation", "outlier", "summary"} else
                                   "segment" if context.kind in {"segment_comparison", "ranking", "distribution", "decile"} else "time")
                agent.plan.requires_sql = True
                agent.plan.question_type = ("lookup" if context.kind in {"lookup", "ranking", "distribution", "decile", "correlation", "outlier", "summary"}
                                            else "comparison" if context.kind == "trend" else "diagnosis")
                if context.kind in {"lookup", "ranking", "correlation", "outlier", "summary"} or (context.kind == "segment_comparison" and not context.dimension):
                    agent.plan.python_operation = None
                elif context.kind == "distribution":
                    agent.plan.python_operation = "share"
                elif context.kind == "decile":
                    agent.plan.python_operation = "decile"
                elif context.kind == "trend" or (context.kind == "period_change" and not context.dimension):
                    agent.plan.python_operation = "percent_change"
                else:
                    agent.plan.python_operation = "attribution"
                agent.plan.chart_type = chart_type
                agent.plan.answer_requirements = contract.goals
                agent.plan.analysis_steps = [item["reasoning"] for item in pipeline]
                if context.kind == "trend" and context.dimension:
                    agent.plan.dimensions = [context.time_grain or "month", context.dimension]
                required_pipeline = list(pipeline)
                self._track("planning", agent, "scoped")
                prepared = self._pipeline_decision(pipeline.pop(0))
                return {"agent": agent, "decision": prepared, "timings_ms": timings,
                        "pipeline": pipeline, "scoped_analysis": True,
                        "required_pipeline": required_pipeline, "pipeline_start_round": agent.current_round,
                        "next_action": prepared.action,
                        "round_limit": max(state["round_limit"], agent.current_round + len(required_pipeline))}
            agent.task.metric = agent.plan.metric
            self._track("planning", agent, "lookup" if agent.plan.requires_sql else "concept")
            agent.task.comparison = agent.plan.comparison
            agent.task.time_range = agent.plan.time_range
            agent.task.dimensions = agent.plan.dimensions
            if agent.task.kind in {"period_change", "segment_comparison", "trend"}:
                agent.plan.requires_sql = True
                agent.plan.question_type = (
                    "diagnosis" if agent.task.kind != "trend" else "comparison"
                )
                agent.plan.python_operation = (
                    "attribution" if agent.task.kind != "trend" else "percent_change"
                )
        return None

    def _resolve_decision(self, agent, decision, state, timings):
        """Apply completion gates separately from planning and model transport."""
        agent.feedback = ""
        if state.get("scoped_analysis") and decision.action != "finish":
            self._stop("invalid_report")
            agent.feedback = "当前固定流程已执行，但模型未返回最终报告；保留已有结果，不再追加工具或重复调用。"
            return {"agent": agent, "timings_ms": timings, "report_mode": "incomplete", "next_action": "report"}
        if decision.action == "finish":
            feedback = self._finish_feedback(agent)
            if feedback:
                agent.feedback = feedback
                return {"agent": agent, "timings_ms": timings, "next_action": "decide"}
            return {
                "agent": agent, "decision": decision, "timings_ms": timings,
                "next_action": "report", "report_mode": "finish",
            }
        return {
            "agent": agent, "decision": decision, "timings_ms": timings,
            "next_action": decision.action,
        }

    @staticmethod
    def _pipeline_decision(item: dict) -> SimpleNamespace:
        return SimpleNamespace(
            action=item["action"], reasoning=item["reasoning"], sql=item.get("sql"),
            python=PythonAnalysisRequest(**item["python"]) if item.get("python") else None,
            chart=ChartRequest(**item["chart"]) if item.get("chart") else None,
            content="", plan=None,
        )

    def _route_after_decision(self, state: WorkflowState) -> str:
        if state["agent"].current_round >= state["round_limit"] and state["next_action"] == "decide":
            return "report"
        return state["next_action"]

    def _sql_node(self, state: WorkflowState) -> dict:
        self._track("sql", state["agent"])
        if getattr(self, "_emit", None):
            self._emit("status", {"message": "正在查询数据…"})
        agent = state["agent"].model_copy(deep=True)
        decision = state["decision"]
        sql = decision.sql or ""
        normalized = " ".join(sql.split()).lower()
        failed_sql = set(state["failed_sql"])
        if normalized in failed_sql:
            agent.feedback = "这条 SQL 已经执行失败，请修改后再试，不要原样重复。"
            return {"agent": agent}
        if len(failed_sql) >= 2:
            agent.feedback = "SQL 已失败两次，请停止查询并说明目前无法确定的结论。"
            return {"agent": agent}
        started = perf_counter()
        semantic_error = semantic_sql_error(sql)
        if semantic_error:
            observation = f"工具错误：{semantic_error}"
            failed_sql.add(normalized)
        else:
            try:
                observation = self.sql_tool.run(sql)
            except SQLToolError as exc:
                observation = f"工具错误：{exc}"
                failed_sql.add(normalized)
        timings = dict(state["timings_ms"])
        timings[f"sql_round_{agent.current_round}"] = round((perf_counter() - started) * 1000)
        agent.steps.append(AgentStep(
            round=agent.current_round, action="sql", reasoning=decision.reasoning,
            tool_input=sql, observation=observation,
        ))
        result = {"agent": agent, "failed_sql": failed_sql, "timings_ms": timings}
        if self._is_truncated_sql_result(agent.steps[-1]):
            self._stop("row_limit")
            result["report_mode"] = "truncated"
        return result

    def _python_node(self, state: WorkflowState) -> dict:
        self._track("python", state["agent"])
        if getattr(self, "_emit", None):
            self._emit("status", {"message": "正在计算与核对分析结果…"})
        agent = state["agent"].model_copy(deep=True)
        decision = state["decision"]
        request = decision.python
        if request is None:
            raise LLMError("模型没有提供 Python 分析参数。")
        started = perf_counter()
        try:
            observation = self.python_tool.run(request, agent.steps)
        except PythonToolError as exc:
            observation = f"工具错误：{exc}"
        timings = dict(state["timings_ms"])
        timings[f"python_round_{agent.current_round}"] = round((perf_counter() - started) * 1000)
        agent.steps.append(AgentStep(
            round=agent.current_round, action="python", reasoning=decision.reasoning,
            tool_input=request.model_dump_json(), observation=observation,
        ))
        return {"agent": agent, "timings_ms": timings}

    def _chart_node(self, state: WorkflowState) -> dict:
        self._track("chart", state["agent"])
        if getattr(self, "_emit", None):
            self._emit("status", {"message": "正在生成图表…"})
        agent = state["agent"].model_copy(deep=True)
        decision = state["decision"]
        request = decision.chart
        if request is None:
            raise LLMError("模型没有提供图表参数。")
        started = perf_counter()
        try:
            observation = self.chart_tool.run(request, agent.steps)
            agent.charts.append(json.loads(observation)["chart_url"])
        except ChartToolError as exc:
            observation = f"工具错误：{exc}"
        timings = dict(state["timings_ms"])
        timings[f"chart_round_{agent.current_round}"] = round((perf_counter() - started) * 1000)
        agent.steps.append(AgentStep(
            round=agent.current_round, action="chart", reasoning=decision.reasoning,
            tool_input=request.model_dump_json(), observation=observation,
        ))
        return {"agent": agent, "timings_ms": timings}

    def _route_after_tool(self, state: WorkflowState) -> str:
        if state.get("report_mode") == "truncated" or state["agent"].current_round >= state["round_limit"]:
            return "report"
        return "decide"

    def _report_node(self, state: WorkflowState) -> dict:
        agent = state["agent"]
        mode = state.get("report_mode", "incomplete")
        if mode == "finish":
            feedback = self._pipeline_feedback(state) or self._finish_feedback(agent)
            if feedback:
                mode = "incomplete"
                agent = agent.model_copy(update={"feedback": feedback})
        finished = mode == "finish"
        if mode == "truncated":
            answer = TRUNCATED_RESULT_MESSAGE
        elif mode == "clarification":
            answer = state["clarification"]
        elif finished:
            answer = state["decision"].content
            if (getattr(self, "_emit", None) and not self._streamed_report
                    and not self._finish_feedback(agent)):
                self._emit("status", {"message": "正在生成回答…"})
                started = perf_counter()
                try:
                    self._track("report", agent)
                    answer = self.llm.stream_report(agent, state["round_limit"], state["schema"], self._emit)
                except LLMError as exc:
                    self._stop(exc.code)
                    if exc.code == "stream_cancelled" or not any(self._has_usable_sql_rows(step) for step in agent.steps if step.action == "sql"):
                        raise
                    mode, finished = "incomplete", False
                    agent.feedback = f"查询与计算结果已保留，但文字报告未完成：{exc}"
                    answer = self._partial_report(agent)
                finally:
                    state["timings_ms"]["llm_stream_report"] = round((perf_counter() - started) * 1000)
            if finished and agent.plan.requires_sql and not any(
                self._has_query_result(step) for step in agent.steps if step.action == "sql"
            ):
                answer = "数据库查询未成功，当前没有可核对的数据，无法得出结论。请查看 steps 中的工具错误。"
            elif finished and agent.plan.python_operation == "attribution" and self._failed_attribution(agent):
                answer = "归因核验未通过，无法得出可信的维度贡献结论。请查看 steps 中的工具错误，并核对两条 SQL 的对比范围、指标口径和分组是否一致。"
            if finished:
                answer = ensure_evidence_sections(answer, agent)
        else:
            if any(self._has_usable_sql_rows(step) for step in agent.steps if step.action == "sql"):
                answer = self._partial_report(agent)
            elif agent.task.kind != "lookup":
                answer = "深度分析尚未取得足够的可核对证据，不能给出完整结论。"
                if agent.feedback:
                    answer += f"当前缺口：{agent.feedback}"
            else:
                answer = (
                    "Agent 已达到最大执行轮数，未能取得足够证据完成分析。"
                    if agent.feedback else "Agent 已达到最大执行轮数，未能生成最终报告。"
                )
                if agent.feedback:
                    answer += f"当前缺口：{agent.feedback}"
        timings = dict(state["timings_ms"])
        timings["total"] = round((perf_counter() - state["started"]) * 1000)
        context = agent.analysis_context if state.get("scoped_analysis") and finished else None
        self._trace.tools = tool_receipts(agent.steps)
        self._trace.last_round = agent.current_round
        if not finished and mode != "clarification" and self._trace.stop_reason is None:
            self._stop("tool_error" if any(tool.outcome == "error" for tool in self._trace.tools) else "round_limit")
        self._trace.status = "completed" if finished else "clarification" if mode == "clarification" else "incomplete"
        if finished:
            self._trace.stage = "complete"
        suggestions = (
            followup_suggestions(context, state["schema"], self._verified_attribution(agent.steps))
            if finished and context else []
        )
        return {"response": AnalysisResponse(
            answer=answer, plan=agent.plan, rounds=agent.current_round,
            finished=finished, steps=agent.steps, charts=agent.charts,
            timings_ms=timings,
            analysis_context=context,
            suggestions=suggestions,
            requires_clarification=mode == "clarification",
        )}

    @staticmethod
    def _pipeline_feedback(state: WorkflowState) -> str:
        for offset, item in enumerate(state.get("required_pipeline", [])):
            step = next((entry for entry in state["agent"].steps
                         if entry.round == state["pipeline_start_round"] + offset
                         and entry.action == item["action"]), None)
            if step is None:
                return f"尚未完成：{item['reasoning']}。"
            operation = item.get("python", {}).get("operation") or item.get("chart", {}).get("chart_type")
            valid = (DataAnalysisAgent._has_query_result(step) if item["action"] == "sql"
                     else DataAnalysisAgent._has_tool_result(step, operation))
            if not valid:
                detail = step.observation if step.observation.startswith("工具错误：") else "未取得有效工具结果。"
                return f"尚未完成：{item['reasoning']}。{detail}"
        return ""

    @staticmethod
    def _partial_report(agent: AgentState) -> str:
        return partial_report(agent)

    @staticmethod
    def _has_query_result(step: AgentStep) -> bool:
        try:
            result = json.loads(step.observation)
            return isinstance(result, dict) and isinstance(result.get("rows"), list)
        except json.JSONDecodeError:
            return False

    @staticmethod
    def _is_truncated_sql_result(step: AgentStep) -> bool:
        if step.action != "sql":
            return False
        try:
            result = json.loads(step.observation)
        except json.JSONDecodeError:
            return False
        return isinstance(result, dict) and result.get("truncated") is True

    @staticmethod
    def _has_usable_sql_rows(step: AgentStep) -> bool:
        try:
            result = json.loads(step.observation)
            return (
                isinstance(result, dict)
                and isinstance(result.get("rows"), list)
                and bool(result["rows"])
                and not result.get("truncated")
            )
        except json.JSONDecodeError:
            return False

    @staticmethod
    def _has_tool_result(step: AgentStep, operation: str) -> bool:
        try:
            result = json.loads(step.observation)
            if not isinstance(result, dict):
                return False
            if step.action == "python":
                return result.get("operation") == operation and "result" in result
            if step.action == "chart":
                return result.get("chart_type") == operation and "chart_url" in result
        except json.JSONDecodeError:
            pass
        return False

    @staticmethod
    def _failed_attribution(state: AgentState) -> bool:
        if any(
            DataAnalysisAgent._has_tool_result(step, "attribution")
            for step in state.steps
        ):
            return False
        for step in state.steps:
            if step.action != "python" or not step.observation.startswith("工具错误："):
                continue
            try:
                if json.loads(step.tool_input).get("operation") == "attribution":
                    return True
            except (json.JSONDecodeError, AttributeError):
                continue
        return False

    @staticmethod
    def _verified_attribution(steps: list[AgentStep]) -> dict | None:
        for step in reversed(steps):
            if step.action != "python":
                continue
            try:
                observation = json.loads(step.observation)
            except json.JSONDecodeError:
                continue
            if not isinstance(observation, dict) or observation.get("operation") != "attribution":
                continue
            result = observation.get("result")
            if not isinstance(result, dict) or result.get("reconciled") is not True:
                continue
            try:
                baseline = Decimal(result["baseline_total"])
                current = Decimal(result["current_total"])
                change = Decimal(result["total_change"])
                if (
                    baseline.is_finite() and current.is_finite() and change.is_finite()
                    and baseline + change == current
                    and isinstance(result["details"], list)
                    and result["baseline_period"] and result["current_period"]
                ):
                    return result
            except (KeyError, InvalidOperation, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _has_verified_trend(steps: list[AgentStep]) -> bool:
        for step in steps:
            if step.action != "python":
                continue
            try:
                observation = json.loads(step.observation)
            except json.JSONDecodeError:
                continue
            if not isinstance(observation, dict) or observation.get("operation") != "percent_change":
                continue
            points = observation.get("result")
            if isinstance(points, list) and len(points) >= 2 and any(
                isinstance(point, dict) and point.get("difference") is not None
                for point in points[1:]
            ):
                return True
        return False

    @staticmethod
    def _finish_feedback(state: AgentState) -> str:
        plan = state.plan
        if plan is None:
            return "缺少分析计划。"
        sql_steps = [step for step in state.steps if step.action == "sql"]
        if any(DataAnalysisAgent._is_truncated_sql_result(step) for step in sql_steps):
            return TRUNCATED_RESULT_MESSAGE
        if plan.requires_sql and not sql_steps:
            return "该问题需要数据库证据，必须先调用 SQL Tool，不能仅凭表结构结束。"
        if plan.requires_sql:
            feedback = grain_feedback(state.question, sql_steps)
            if feedback:
                return feedback
        if state.task.kind in {"period_change", "segment_comparison"}:
            usable = [step for step in sql_steps if DataAnalysisAgent._has_usable_sql_rows(step)]
            context = plan.analysis_context
            totals_only = context is not None and not context.dimension
            if totals_only:
                expected = ({context.baseline.label(), context.current.label()}
                            if context.kind == "period_change"
                            else {context.baseline_group, context.current_group})
                if not any(
                    len(json.loads(step.observation)["rows"]) == 2
                    and {row.get("period") for row in json.loads(step.observation)["rows"]} == expected
                    for step in usable
                ):
                    return "必须取得用户指定的两个对比对象总额；缺少任一对象时不能完成比较。"
                if context.kind == "period_change" and not DataAnalysisAgent._has_verified_trend(state.steps):
                    return "请计算两个指定时期之间的差额与变化率后再结束。"
            elif len(usable) < 2:
                return "诊断需要两份独立、未截断的比较对象总额与维度明细 SQL 结果。"
            elif DataAnalysisAgent._verified_attribution(state.steps) is None:
                return "请调用 attribution 核对两个时期或群体的独立总额与维度明细，未核验前不能结束。"
        if state.task.kind == "trend":
            if not any(
                DataAnalysisAgent._has_usable_sql_rows(step)
                and len(json.loads(step.observation)["rows"]) >= 2
                for step in sql_steps
            ):
                return "趋势分析至少需要两个按时间排序、未截断的数据点。"
            if not DataAnalysisAgent._has_verified_trend(state.steps):
                return "请先用 percent_change 核算相邻时间点的变化，再形成趋势结论。"
            context = plan.analysis_context
            if context and context.dimension and not any(
                DataAnalysisAgent._has_tool_result(step, "sequential_attribution")
                and json.loads(step.observation).get("result", {}).get("reconciled") is True
                for step in state.steps if step.action == "python"
            ):
                return "时间变化已查询，但维度拆解尚未逐期核对独立总额并计算贡献，不能作为完整分析结束。"
        if state.task.kind == "decile":
            if not any(
                DataAnalysisAgent._has_tool_result(step, "decile")
                and json.loads(step.observation).get("result", {}).get("reconciled") is True
                for step in state.steps if step.action == "python"
            ):
                return "十分位分层必须核对各层合计等于独立总额，未核验前不能结束。"
        if plan.python_operation:
            if any(DataAnalysisAgent._has_usable_sql_rows(step) for step in sql_steps):
                if not any(
                    DataAnalysisAgent._has_tool_result(step, plan.python_operation)
                    for step in state.steps
                ):
                    if (
                        plan.python_operation == "attribution"
                        and DataAnalysisAgent._failed_attribution(state)
                    ):
                        return ""
                    return (
                        f"已有可分析的 SQL 数据，必须先调用 Python Tool 的 "
                        f"{plan.python_operation}，再给出结论。"
                    )
        if any("实际查询订单量" in goal or "实际查询同范围供货" in goal for goal in plan.answer_requirements):
            if not any(DataAnalysisAgent._has_tool_result(step, "driver_check")
                       and json.loads(step.observation).get("result", {}).get("reconciled") is True
                       for step in state.steps if step.action == "python"):
                return "本轮要求订单或供货查证，尚未取得与独立总额核对的辅助结果，不能标记完整完成。"
        if plan.chart_type:
            if any(DataAnalysisAgent._has_usable_sql_rows(step) for step in sql_steps):
                if not any(
                    DataAnalysisAgent._has_tool_result(step, plan.chart_type)
                    for step in state.steps
                ):
                    return f"已有 SQL 数据，必须先生成 {plan.chart_type} 图，才能完成回答。"
        return ""
