"""Regression evidence from the four-turn real acceptance, with no API calls."""
import json
import unittest
from types import SimpleNamespace

from app.agent.context import AnalysisContext
from app.agent.prompts import REPORT_PROMPT, build_user_prompt
from app.agent.report_facts import report_facts
from app.agent.state import AgentState, AgentStep


def sql_step(rows, truncated=False):
    return AgentStep(round=1, action="sql", reasoning="test", tool_input="SELECT",
                     observation=json.dumps({"rows": rows, "truncated": truncated}))


class ReportFactsTests(unittest.TestCase):
    def test_exclusive_end_and_exact_differences(self):
        context = AnalysisContext(kind="trend", source_table="public.demo_ecommerce_orders",
                                  metric_label="销售额", metric_expression="sales_amount", time_column="order_date",
                                  current={"start": "2026-01-01", "end": "2026-03-01"})
        state = SimpleNamespace(plan=SimpleNamespace(analysis_context=context), steps=[sql_step([
            {"period": "2026-01", "amount": "666000"},
            {"period": "2026-02", "amount": "652300"},
        ])])
        facts = json.loads(report_facts(state))
        self.assertEqual(facts["query_ranges"][0]["end_exclusive"], "2026-03-01")
        self.assertEqual(facts["monthly_evidence"][0]["returned_months"], ["2026-01", "2026-02"])
        self.assertEqual(facts["monthly_evidence"][0]["adjacent_changes"][0]["difference"], "-13700")
        state.steps.append(sql_step([
            {"period": "2026-02", "amount": "652300"},
            {"period": "2026-03", "amount": "702600"},
        ]))
        self.assertEqual(json.loads(report_facts(state))["monthly_evidence"][1]["observed_max_minus_min"], "50300")

    def test_unsafe_series_not_aggregated(self):
        for rows, truncated in [([], False), ([{"period": "2026-01", "amount": 1}] * 2, False),
                                ([{"period": "2026-01", "amount": "NaN"}] * 2, False),
                                ([{"period": "2026-01", "amount": 1, "dimension": "线上"}] * 2, False),
                                ([{"period": "2026-01", "amount": 1}, {"period": "2026-02", "amount": 2}], True)]:
            with self.subTest(rows=rows, truncated=truncated):
                facts = json.loads(report_facts(SimpleNamespace(plan=None, steps=[sql_step(rows, truncated)])))
                self.assertEqual(facts["monthly_evidence"], [])

    def test_extreme_months_ties_and_nonconsecutive_months_are_explicit(self):
        for rows, maxima, minima, is_month_over_month in [
            ([{"period": "2026-01", "amount": 666000},
              {"period": "2026-02", "amount": 652300},
              {"period": "2026-03", "amount": 702600}], ["2026-03"], ["2026-02"], True),
            ([{"period": "2026-01", "amount": 10},
              {"period": "2026-03", "amount": 10}], ["2026-01", "2026-03"], ["2026-01", "2026-03"], False),
        ]:
            with self.subTest(months=maxima):
                evidence = json.loads(report_facts(SimpleNamespace(plan=None, steps=[sql_step(rows)])))["monthly_evidence"][0]
                self.assertEqual(evidence["maximum"]["months"], maxima)
                self.assertEqual(evidence["minimum"]["months"], minima)
                self.assertEqual(evidence["adjacent_changes"][0]["is_month_over_month"], is_month_over_month)

    def test_contribution_scope_not_confused_with_own_rate(self):
        step = AgentStep(round=2, action="python", reasoning="test", tool_input="{}",
                         observation=json.dumps({"operation": "attribution", "result": {
                             "reconciled": True, "total_change": "-13700"}}))
        facts = json.loads(report_facts(SimpleNamespace(plan=None, steps=[step])))
        semantics = facts["contribution_semantics"][0]
        self.assertEqual(semantics["denominator"], "-13700")
        self.assertIn("无筛选的总体归因", semantics["scope"])
        self.assertIn("自身变化率", semantics["interpretation"])

    def test_report_prompt_has_boundaries_without_changing_steps(self):
        step = sql_step([{"month": "2026-01", "sales_amount": 1},
                         {"month": "2026-02", "sales_amount": 2}])
        state = AgentState(question="测试", steps=[step])
        original = state.model_dump()
        prompt = build_user_prompt(state, 7, "schema", report=True)
        self.assertIn('"observed_max_minus_min":"1"', prompt)
        self.assertIn("不能声称 3 月没有数据", REPORT_PROMPT)
        self.assertEqual(state.model_dump(), original)
