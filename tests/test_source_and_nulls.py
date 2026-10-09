"""Small deterministic regressions for source locks, real fields and NULL semantics."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.context import DateRange, ScopeFilter
from app.agent.scoped_analysis import build_pipeline, validate_context
from app.agent.source_scope import resolve_source, sql_source_error
from app.main import app
from tests.test_regression_checks import context, decision, make_agent, plan
from tests.test_sessions import analysis_response


SCHEMA = "public.sales: amount (numeric), 用户数量 (integer)\npublic.other: amount (numeric), 用户数量 (integer)"


class SourceAndNullTests(unittest.TestCase):
    def test_first_selection_rejects_wrong_plan_then_uses_selected_source(self):
        for selected in (None, "public.sales"):
            with self.subTest(selected=selected):
                agent, llm, sql, *_ = make_agent([
                    decision("finish", analysis_plan=plan(analysis_context=context("public.other"))),
                    decision("finish", analysis_plan=plan(analysis_context=context())),
                    decision("finish", content="合计100"),
                ], schema=SCHEMA)
                result = agent.run("使用数据表 public.sales。查询金额合计", source_table=selected)
                self.assertTrue(result.finished)
                self.assertEqual(result.diagnostics.planning_attempts, 2)
                sql.run.assert_called_once()
                self.assertNotIn('"other"', sql.run.call_args.args[0])

    def test_conflicting_or_inaccessible_source_clarifies_before_model(self):
        for question, source in (("查询 public.other 的金额", "public.sales"),
                                 ("查询金额", "public.missing"),
                                 ("查询金额", "public.analysis_sessions")):
            agent, llm, sql, *_ = make_agent([], schema=SCHEMA)
            result = agent.run(question, source_table=source)
            self.assertTrue(result.requires_clarification)
            llm.decide.assert_not_called()
            sql.run.assert_not_called()

    def test_sql_guard_checks_physical_sources_not_cte_names(self):
        for query in ("SELECT * FROM other", "SELECT * FROM sales JOIN other USING(amount)",
                      "WITH sales AS (SELECT * FROM other) SELECT * FROM sales",
                      "SELECT * FROM sales UNION SELECT * FROM other", "SELECT 123"):
            self.assertIsNotNone(sql_source_error(query, "public.sales"))
        self.assertIsNone(sql_source_error("WITH t AS (SELECT * FROM public.sales) SELECT * FROM t", "public.sales"))
        self.assertIsNone(sql_source_error("SELECT amount FROM sales s WHERE EXISTS (SELECT 1 FROM sales b)", "public.sales"))
        self.assertEqual(resolve_source("查询 public.other", SCHEMA, None, context()), "public.other")

    def test_free_sql_path_never_executes_wrong_source(self):
        agent, llm, sql, *_ = make_agent([
            decision("sql", analysis_plan=plan(), sql="SELECT amount FROM other"),
            decision("sql", sql="SELECT amount FROM sales"),
            decision("finish", content="合计100"),
        ], schema=SCHEMA)
        result = agent.run("查询金额", source_table="public.sales")
        self.assertTrue(result.finished)
        sql.run.assert_called_once_with("SELECT amount FROM sales")
        self.assertEqual(result.source_table, "public.sales")
        self.assertIn("其他数据源", result.steps[0].observation)

    def test_switch_drops_old_scope_and_history(self):
        old = context(time_column="date", current=DateRange(start="2026-01-01", end="2026-02-01"),
                      filters=[ScopeFilter(column="channel", values=["旧渠道"])])
        from app.agent.state import ConversationTurn
        agent, llm, sql, *_ = make_agent([
            decision("finish", analysis_plan=plan(analysis_context=context("public.other"))),
            decision("finish", content="合计100"),
        ], schema=SCHEMA)
        result = agent.run("继续查询金额合计", source_table="public.other", analysis_context=old,
                           conversation_history=[ConversationTurn(question="旧问题", answer="旧指标")])
        self.assertTrue(result.finished)
        first_state = llm.decide.call_args_list[0].args[0]
        # Mock retains the mutable state reference; final scope still must contain no old values.
        self.assertIsNone(first_state.analysis_context.current)
        self.assertEqual(first_state.analysis_context.filters, [])
        self.assertEqual(first_state.conversation_history, [])
        self.assertIsNone(result.analysis_context.current)
        self.assertEqual(result.analysis_context.filters, [])

    def test_chinese_business_field_runs_and_absent_field_clarifies(self):
        for field, succeeds in (("用户数量", True), ("退款金额", False)):
            with self.subTest(field=field):
                agent, llm, sql, *_ = make_agent([
                    decision("finish", analysis_plan=plan(analysis_context=context(metric_expression=field, metric_label=field))),
                    decision("finish", content="合计100"),
                ], schema=SCHEMA)
                result = agent.run(f"查询{field}合计", source_table="public.sales")
                self.assertEqual(result.finished, succeeds)
                if succeeds:
                    self.assertIn('"用户数量"', sql.run.call_args.args[0])
                else:
                    self.assertTrue(result.requires_clarification)
                    self.assertIn("退款金额", result.answer)
                    sql.run.assert_not_called()

    def test_actual_missing_field_question_stops_before_model_or_sql(self):
        from app.agent.source_scope import missing_field_error
        self.assertIsNone(missing_field_error("查询销售额合计", "public.sales: 订单金额 (numeric)", "public.sales"))
        question = "查询当前表的退款金额合计。如果没有退款金额字段，请明确说明缺少这个字段，不要用销售额代替。"
        schema = "public.sales: 订单日期 (date), 销售额 (numeric), 用户数量 (integer)"
        agent, llm, sql, *_ = make_agent([], schema=schema)
        result = agent.run(question, source_table="public.sales")
        self.assertTrue(result.requires_clarification)
        self.assertFalse(result.finished)
        self.assertIn("没有字段 退款金额", result.answer)
        llm.decide.assert_not_called()
        sql.run.assert_not_called()

    def test_null_preserving_sql_and_no_valid_samples(self):
        ctx = validate_context(context(kind="outlier"), SCHEMA)
        query = build_pipeline(ctx, 1)[0]["sql"]
        self.assertEqual(query.count('WHEN "amount" IS NULL THEN NULL'), 2)
        self.assertIn('COUNT("amount") AS valid_count', query)
        for emit in (None, lambda *args: None):
            agent, llm, sql, *_ = make_agent([
                decision("finish", analysis_plan=plan(analysis_context=context(kind="outlier"))),
            ])
            sql.run.return_value = json.dumps({"rows": [{"valid_count": 0, "original_amount": None,
                "winsorized_amount": None, "truncated_amount": None}], "truncated": False})
            result = agent.run("按1%/99%分位缩尾处理金额", source_table="public.sales", emit=emit)
            self.assertTrue(result.finished)
            self.assertIn("没有有效", result.answer)
            self.assertEqual(llm.decide.call_count, 1)

    def test_outlier_report_distinguishes_winsorized_and_truncated_samples(self):
        from app.agent.report_presentation import outlier_report
        answer = outlier_report({"valid_count": 2, "retained_count": 0,
            "original_amount": "30", "winsorized_amount": "30", "truncated_amount": "0",
            "below_count": 1, "above_count": 1, "lower_bound": "10.1", "upper_bound": "19.9"}, "销售额")
        self.assertIn("缩尾保留全部 **2**", answer)
        self.assertIn("截尾对照：区间内保留 **0**", answer)
        self.assertIn("全部有效样本被排除", answer)

    def test_http_and_sse_forward_selected_source(self):
        for streaming in (False, True):
            with patch("app.api.analysis.get_settings", return_value=SimpleNamespace(session_database_url=None)), patch(
                "app.api.analysis.DataAnalysisAgent.run", return_value=analysis_response()
            ) as run, patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
                "app.api.analysis.PythonAnalysisTool"
            ), patch("app.api.analysis.ChartTool"):
                response = TestClient(app).post("/api/v1/analyze" + ("/stream" if streaming else ""),
                    json={"question": "查询金额", "source_table": "public.sales"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(run.call_args.kwargs["source_table"], "public.sales")
