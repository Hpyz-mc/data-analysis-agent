"""Bounded answer coverage and supporting checks; no model/network/database calls."""
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from sqlglot import parse_one

from app.agent.answer_contract import build_contract, append_driver_pipeline, append_requested_charts
from app.agent.context import merge_context
from app.agent.loop import DataAnalysisAgent
from app.agent.prompts import build_user_prompt
from app.agent.report_facts import report_facts
from app.agent.scoped_analysis import ScopeError, build_pipeline, validate_context
from app.agent.state import AnalysisPlan, AgentState, AgentStep, ConversationTurn
from app.api.analysis import _usable_for_history
from app.tools.driver_check import driver_check
from app.tools.python_tool import PythonAnalysisTool
from tests.test_analysis_context import SCHEMA, scope


FULL_SCHEMA = SCHEMA + ", order_count (integer), supply_status (text), profit (numeric)"


def records(context):
    base, current = context.baseline.label(), context.current.label()
    totals = [{"period": base, "amount": "200000"}, {"period": current, "amount": "163000"}]
    details = [{"period": p, "dimension": "线下门店", "amount": a} for p, a in ((base, "200000"), (current, "163000"))]
    auxiliary = [dict(row, orders=n, missing_amount=0, invalid_orders=0,
                      stockout_min=d, stockout_max=d, stockout_distinct=1,
                      supply_statuses=[s]) for row, n, d, s in zip(details, (2000, 1630), (0, 6), ("正常", "供应链断货"))]
    return totals, details, auxiliary


class AnswerQualityTests(unittest.TestCase):
    def graph(self, *, bad_aux=False, question="为什么2月销售额比1月下降？按渠道归因并画折线图和贡献图。"):
        context = validate_context(scope(), FULL_SCHEMA)
        totals, details, aux = records(context)
        if bad_aux:
            aux[0]["amount"] = "199999"
        plan = AnalysisPlan(question_type="diagnosis", requires_sql=True, metric="销售额",
                            time_range="1月与2月", comparison="2月比1月", analysis_steps=["归因"],
                            analysis_context=context)
        llm = SimpleNamespace(decide=MagicMock(side_effect=[
            SimpleNamespace(action="finish", reasoning="规划", content="", plan=plan),
            SimpleNamespace(action="finish", reasoning="回答", content="下降主要来自线下门店。", plan=None),
        ]))
        sql = SimpleNamespace(get_schema=lambda: FULL_SCHEMA, run=MagicMock(side_effect=[
            json.dumps({"rows": rows, "truncated": False}) for rows in (totals, details, aux)]))
        chart = SimpleNamespace(run=lambda req, steps: json.dumps({"chart_type": req.chart_type,
            "chart_url": f"/api/v1/charts/{req.chart_type}.png"}))
        result = DataAnalysisAgent(llm, sql, PythonAnalysisTool(), chart).run(question)
        return result, llm, sql

    def test_complete_request_executes_auxiliary_and_both_chart_types(self):
        result, llm, sql = self.graph()
        self.assertTrue(result.finished)
        self.assertTrue(_usable_for_history(result))
        self.assertEqual(sql.run.call_count, 3)
        self.assertEqual(llm.decide.call_count, 2)
        self.assertEqual(len(result.charts), 2)
        self.assertIn("订单量变化效应：-37000.00", result.answer)
        self.assertIn("客单价变化效应：0.00", result.answer)
        self.assertIn("供应链断货", result.answer)
        self.assertIn("不是因果证明", result.answer)
        self.assertNotIn("已核对的量化证据", result.answer)
        state = AgentState(question="为什么销售额下降", plan=result.plan,
                           conversation_history=[ConversationTurn(question="旧问题", answer="旧数字不能作为本轮证据")],
                           steps=result.steps)
        report = build_user_prompt(state, 12, FULL_SCHEMA, report=True)
        self.assertNotIn("旧数字不能作为本轮证据", report)
        self.assertIn("用户问题", report)
        self.assertIn("answer_requirements", build_user_prompt(state, 12, FULL_SCHEMA))
        query = sql.run.call_args_list[2].args[0]
        self.assertNotIn("SUM(\"stockout_days\")", query)
        self.assertIn('MAX("stockout_days")', query)
        total = sql.run.call_args_list[0].args[0]
        self.assertEqual(parse_one(query, read="postgres").args["where"],
                         parse_one(total, read="postgres").args["where"])

    def test_bad_auxiliary_cannot_be_called_complete(self):
        result, llm, _ = self.graph(bad_aux=True)
        self.assertFalse(result.finished)
        self.assertFalse(_usable_for_history(result))
        self.assertIn("辅助销售额与独立总额不一致", result.answer)
        self.assertEqual(llm.decide.call_count, 1)

    def test_unsupported_extra_metric_or_unconfirmed_grain_clarifies(self):
        context = validate_context(scope(), FULL_SCHEMA)
        for question in ("比较销售额、订单量和利润变化", "按渠道归因并画饼图"):
            with self.subTest(question=question), self.assertRaises(ScopeError):
                contract = build_contract(question, context, FULL_SCHEMA)
                append_requested_charts(build_pipeline(context, 1), context, question, 1)
        imported = context.model_copy(update={"source_table": "public.user_sales_20261004_01"})
        schema = FULL_SCHEMA.replace("public.demo_ecommerce_orders", imported.source_table)
        with self.assertRaisesRegex(ScopeError, "互斥记录"):
            build_contract("比较销售额和订单量、客单价", imported, schema)
        contract = build_contract("比较销售额和订单量、客单价；订单量按互斥记录求和", imported, schema)
        self.assertEqual(contract.order_column, "order_count")
        inherited = merge_context(imported.model_copy(update={"confirmed_order_column": None}), imported, "继续按渠道归因，为什么下降")
        self.assertEqual(build_contract("继续按渠道归因，为什么下降", inherited, schema).order_column, "order_count")

    def test_zero_orders_nulls_and_mismatch_do_not_invent_effects(self):
        totals, _, aux = records(scope())
        aux[0]["orders"] = 0
        result = driver_check(aux, totals, with_orders=True)
        self.assertIsNone(result["comparisons"][0]["baseline_aov"])
        self.assertFalse(result["comparisons"][0]["effects_reconciled"])
        for row in (dict(aux[0], invalid_orders=1), dict(aux[0], missing_amount=1)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                driver_check([row, aux[1]], totals, with_orders=True)
        totals, _, aux = records(scope())
        reverse = driver_check(aux, totals, with_orders=True,
                               period_order=[totals[1]["period"], totals[0]["period"]])
        self.assertEqual(reverse["comparisons"][0]["sales_change"], "37000")
        self.assertEqual(reverse["comparisons"][0]["order_effect"], "37000.00")

    def test_followup_preserves_table_metric_ranges_and_filters(self):
        previous = scope(filters=[{"column": "channel", "values": ["线下门店"]}])
        proposal = scope(kind="trend", source_table="public.wrong", metric_expression="profit", metric_label="利润",
                         baseline=None, current={"start": "2026-01-01", "end": "2026-04-01"},
                         filters=[{"column": "channel", "values": ["线上商城"]}])
        merged = merge_context(proposal, previous, "沿用当前表、指标和筛选条件，查看覆盖两个比较时期的月度走势")
        self.assertEqual(merged.source_table, previous.source_table)
        self.assertEqual(merged.metric_expression, previous.metric_expression)
        self.assertEqual(merged.current.end, previous.current.end)
        self.assertEqual(merged.filters, previous.filters)

    def test_uniform_rates_are_not_unique_category_anomalies(self):
        step = AgentStep(round=1, action="python", reasoning="核验", tool_input="{}", observation=json.dumps({
            "operation": "attribution", "result": {"reconciled": True, "total_change": "-37000", "details": [
                {"dimension": "电脑", "baseline_value": "110000", "current_value": "89650", "change": "-20350"},
                {"dimension": "家电", "baseline_value": "60000", "current_value": "48900", "change": "-11100"},
                {"dimension": "服饰", "baseline_value": "30000", "current_value": "24450", "change": "-5550"},
            ]}}))
        facts = json.loads(report_facts(AgentState(question="为什么下降", steps=[step])))
        self.assertEqual(Decimal(facts["contribution_semantics"][0]["uniform_own_change_percent"]), Decimal("-18.5"))

    def test_clarification_keeps_original_multi_metric_request(self):
        llm = SimpleNamespace(decide=MagicMock(side_effect=RuntimeError("inspect planning")))
        sql = SimpleNamespace(get_schema=lambda: FULL_SCHEMA)
        history = [ConversationTurn(question="比较2026年2月与1月销售额、订单量和客单价，按渠道归因",
                                    answer="为了准确分析，请先确认分析范围：请确认订单数可按互斥记录求和。")]
        with self.assertRaisesRegex(RuntimeError, "inspect planning"):
            DataAnalysisAgent(llm, sql, PythonAnalysisTool(), SimpleNamespace()).run("确认可按互斥记录求和", history)
        self.assertIn("订单量和客单价", llm.decide.call_args.args[0].question)
        self.assertIn("用户补充：确认", llm.decide.call_args.args[0].question)


if __name__ == "__main__":
    unittest.main()
