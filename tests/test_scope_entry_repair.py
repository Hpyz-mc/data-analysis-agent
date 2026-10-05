"""Regressions from the ten-question uploaded-data acceptance, offline only."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agent.analysis_policy import build_analysis_task
from app.agent.answer_contract import build_contract
from app.agent.context import AnalysisContext, ScopeFilter
from app.agent.loop import DataAnalysisAgent
from app.agent.prompts import PLANNING_PROMPT
from app.agent.report_facts import report_facts
from app.agent.scoped_analysis import ScopeError, build_pipeline, validate_context
from app.agent.state import AgentState, AnalysisPlan
from app.llm.deepseek import DeepSeekClient
from app.tools.python_tool import PythonAnalysisTool
from tests.test_llm_retry import decision_json, model_response


SCHEMA = "public.uploaded_orders: 下单日期 (date), 订单金额 (numeric), 销售渠道 (text), 一级品类 (text)"


def scope(**changes):
    fields = dict(kind="period_change", source_table="public.uploaded_orders",
                  metric_label="订单金额", metric_expression="订单金额", time_column="下单日期",
                  baseline={"start": "2026-01-01", "end": "2026-02-01"},
                  current={"start": "2026-02-01", "end": "2026-03-01"}, dimension="销售渠道")
    fields.update(changes)
    return AnalysisContext(**fields)


def plan(context):
    return AnalysisPlan(question_type="comparison", requires_sql=True, metric="订单金额求和",
                        time_range="指定月份", comparison="指定比较", analysis_steps=["查询核验"],
                        analysis_context=context)


def run_graph(question, context, rows, previous=None, *, streaming=False):
    llm = SimpleNamespace(decide=MagicMock(side_effect=[
        SimpleNamespace(action="finish", reasoning="范围", content="", plan=plan(context)),
        SimpleNamespace(action="finish", reasoning="报告", content="基于本轮数据回答。", plan=None),
    ]))
    llm.stream_report = MagicMock(return_value="基于本轮数据回答。")
    sql = SimpleNamespace(get_schema=lambda: SCHEMA, run=MagicMock(side_effect=[
        json.dumps({"rows": items, "truncated": False}) for items in rows]))
    chart = SimpleNamespace(run=lambda req, steps: json.dumps(
        {"chart_type": req.chart_type, "chart_url": "/api/v1/charts/offline.png"}))
    result = DataAnalysisAgent(llm, sql, PythonAnalysisTool(), chart).run(
        question, analysis_context=previous, emit=(lambda *args: None) if streaming else None)
    return result, llm, sql


class ScopeEntryRepairTests(unittest.TestCase):
    def test_monthly_lookup_and_sparse_comparison_have_structured_routes(self):
        for question in ("查询2026年1至3月每月订单金额，最高最低相差多少，画折线图",
                         "新问题：查询2026年1至3月每月订单金额"):
            self.assertEqual(build_analysis_task(question).kind, "trend")
        self.assertEqual(build_analysis_task("只查1月与3月金额，计算变化额与变化率").kind, "period_change")
        self.assertEqual(build_analysis_task("为什么A地区比B地区高？").kind, "segment_comparison")
        self.assertNotEqual(build_analysis_task("继续看渠道代理，沿用比较月份，按一级品类下钻").kind,
                            "segment_comparison")
        self.assertNotEqual(build_analysis_task("回到总体，沿用比较时期，按销售渠道归因").kind,
                            "segment_comparison")

    def test_followup_drill_and_reset_keep_periods_and_real_filters(self):
        for reset in (False, True):
            previous = scope(filters=[ScopeFilter(column="销售渠道", values=["渠道代理"])]) if reset else scope()
            proposed = scope(dimension="销售渠道" if reset else "一级品类", filters=[] if reset else [
                ScopeFilter(column="销售渠道", values=["渠道代理"])])
            question = ("取消全部筛选，回到总体，沿用当前表、订单金额和比较月份，按销售渠道归因" if reset else
                        "继续看渠道代理，沿用刚才的订单金额和比较月份，按一级品类下钻")
            labels = [previous.baseline.label(), previous.current.label()]
            totals = [{"period": label, "amount": amount} for label, amount in zip(labels, (100, 80))]
            detail = [dict(row, dimension="企业级数码设备") for row in totals]
            result, llm, sql = run_graph(question, proposed, [totals, detail], previous)
            self.assertTrue(result.finished)
            self.assertEqual(result.analysis_context.kind, "period_change")
            self.assertEqual(result.analysis_context.baseline, previous.baseline)
            self.assertEqual(result.analysis_context.filters, proposed.filters)
            self.assertEqual(llm.decide.call_count, 2)
            query = sql.run.call_args_list[0].args[0]
            self.assertEqual("'渠道代理'" in query, not reset)

    def test_totals_comparison_needs_both_periods_but_not_a_dimension(self):
        context = scope(dimension=None, current={"start": "2026-03-01", "end": "2026-04-01"})
        for question in ("按销售渠道归因", "为什么订单金额下降？"):
            with self.assertRaises(ScopeError):
                build_contract(question, context, SCHEMA)
        totals = [{"period": p.label(), "amount": a} for p, a in zip(
            (context.baseline, context.current), (288260088, 252929487))]
        for streaming in (False, True):
            for missing in (False, True):
                result, llm, sql = run_graph("新问题：只查1月与3月订单金额，计算变化额与变化率，能否称月环比？",
                                            context, [totals[1:] if missing else totals], streaming=streaming)
                self.assertEqual(result.finished, not missing)
                self.assertFalse(result.requires_clarification)
                sql.run.assert_called_once()
                self.assertEqual(llm.decide.call_count + llm.stream_report.call_count, 1 if missing else 2)
                if not missing:
                    points = json.loads(result.steps[1].observation)["result"]
                    self.assertEqual(str(points[1]["difference"]), "-35330601")
                    self.assertEqual(result.plan.python_operation, "percent_change")
                    facts = json.loads(report_facts(AgentState(question="比较", plan=result.plan, steps=result.steps)))
                    self.assertFalse(facts["period_comparison"]["is_month_over_month"])
                    self.assertEqual(facts["period_comparison"]["month_interval"], 2)
                query = sql.run.call_args.args[0]
                self.assertIn(" OR ", query)
                self.assertIn("< DATE '2026-02-01'", query)
                self.assertIn(">= DATE '2026-03-01'", query)

    def test_reversed_comparison_orders_baseline_before_current(self):
        context = validate_context(scope(dimension=None,
            baseline={"start": "2026-03-01", "end": "2026-04-01"},
            current={"start": "2026-01-01", "end": "2026-02-01"}), SCHEMA)
        query = build_pipeline(context, 1)[0]["sql"]
        self.assertIn("ORDER BY CASE WHEN", query)
        self.assertIn("= '" + context.baseline.label() + "' THEN 0 ELSE 1 END", query)

    def test_monthly_problem_cannot_silently_fall_back_to_free_lookup(self):
        context = scope(kind="trend", baseline=None, dimension=None, time_grain="month",
                        current={"start": "2026-01-01", "end": "2026-04-01"})
        totals = [{"period": p, "amount": a} for p, a in (
            ("2026-01", 288260088), ("2026-02", 194847786), ("2026-03", 252929487))]
        for prefix in ("", "新问题："):
            result, llm, _ = run_graph(prefix + "查询1至3月每月订单金额，哪个月最高最低，画折线图", context, [totals])
            self.assertTrue(result.finished)
            self.assertEqual(result.diagnostics.route, "scoped")
            self.assertEqual(llm.decide.call_count, 2)
            self.assertEqual([s.action for s in result.steps], ["sql", "python", "chart"])
        missing = plan(context).model_copy(update={"analysis_context": None, "question_type": "lookup"})
        llm = SimpleNamespace(decide=MagicMock(return_value=SimpleNamespace(plan=missing, action="sql", sql="SELECT 1")))
        sql = SimpleNamespace(get_schema=lambda: SCHEMA, run=MagicMock())
        result = DataAnalysisAgent(llm, sql, PythonAnalysisTool(), None).run("查询每月订单金额")
        self.assertFalse(result.finished)
        self.assertEqual(result.diagnostics.stop_reason, "planning_limit")
        sql.run.assert_not_called()

    def test_real_client_uses_scope_only_planning_prompt(self):
        context = scope(kind="trend", baseline=None, dimension=None, time_grain="month")
        client = object.__new__(DeepSeekClient)
        client.model, client.thinking, client.client = "offline", "disabled", MagicMock()
        client.client.chat.completions.create.return_value = model_response(decision_json(
            "finish", plan(context).model_dump(mode="json")))
        state = AgentState(question="查询每月订单金额", task=build_analysis_task("查询每月订单金额"))
        client.decide(state, 7, SCHEMA)
        messages = client.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0]["content"], PLANNING_PROMPT)
        self.assertIn("本轮仅提交完整 plan.analysis_context", messages[1]["content"])
        self.assertEqual(len(client.usage_calls), 1)


if __name__ == "__main__":
    unittest.main()
