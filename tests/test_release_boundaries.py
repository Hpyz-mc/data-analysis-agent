"""Small offline release regressions; no real SQL, deletion or model calls."""

import unittest
from unittest.mock import MagicMock, patch

from app.agent.context import DateRange
from app.agent.scoped_analysis import build_pipeline, validate_context
from app.agent.source_scope import sql_source_error
from app.agent.state import ConversationTurn
from app.tools.sql_tool import SQLTool, SQLToolError
from app.tools.sql_validator import SQLValidationError, validate_read_only_sql
from tests.test_regression_checks import context, decision, make_agent, plan


SCHEMA = "public.sales: amount (numeric), units (integer), order_date (date), channel (text)\npublic.other: amount (numeric)"


class ReleaseBoundaryTests(unittest.TestCase):
    def test_function_gate_blocks_dynamic_export_and_unknown_functions_before_connection(self):
        queries = [
            f"SELECT {name}('public.other', false, false, '') FROM public.sales"
            for name in ("query_to_xml", "table_to_xml", "schema_to_xml", "query_to_xmlschema",
                         "table_to_xml_and_xmlschema", "schema_to_xml_and_xmlschema")
        ] + [
            "SELECT pg_catalog.query_to_xml('SELECT * FROM public.system_upload_records', false, false, '') FROM sales",
            'SELECT "query_to_xml"(\'SELECT 1\', false, false, \'\') FROM sales',
            "SELECT database_to_xml(false, false, '') FROM sales",
            "SELECT set_config('search_path', 'public', true) FROM sales",
            "SELECT pg_sleep(30) FROM sales",
            "SELECT public.sum(amount) FROM sales",
            "SELECT unknown_helper(amount) FROM sales",
        ]
        tool = object.__new__(SQLTool)
        with patch.object(tool, "_connect", MagicMock()) as connect:
            for query in queries:
                with self.subTest(query=query):
                    with self.assertRaises(SQLValidationError):
                        validate_read_only_sql(query)
                    self.assertIsNotNone(sql_source_error(query, "public.sales"))
                    with self.assertRaises(SQLToolError):
                        tool.run(query)
            connect.assert_not_called()

    def test_agent_never_passes_dynamic_query_to_tool(self):
        bad = "SELECT query_to_xml('SELECT SUM(amount) FROM public.other', false, false, '') FROM public.sales"
        agent, _, sql, *_ = make_agent([
            decision("sql", analysis_plan=plan(), sql=bad),
            decision("sql", sql="SELECT SUM(amount) AS amount FROM public.sales"),
            decision("finish", content="合计100"),
        ], schema=SCHEMA)
        result = agent.run("查询金额合计", source_table="public.sales")
        self.assertTrue(result.finished)
        sql.run.assert_called_once_with("SELECT SUM(amount) AS amount FROM public.sales")
        self.assertIn("允许的分析函数", result.steps[0].observation)

    def test_existing_scoped_pipelines_remain_valid_under_function_allowlist(self):
        periods = dict(time_column="order_date", baseline=DateRange(start="2026-01-01", end="2026-02-01"),
                       current=DateRange(start="2026-02-01", end="2026-03-01"))
        cases = [("lookup", {}), ("summary", {}), ("outlier", {}),
                 ("correlation", {"correlate_column": "units"}),
                 ("ranking", {"dimension": "channel", "limit_n": 5}),
                 ("distribution", {"dimension": "channel"}),
                 ("decile", {"rank_column": "amount"}),
                 ("trend", {"time_column": "order_date", "time_grain": "month",
                            "current": DateRange(start="2026-01-01", end="2026-04-01")}),
                 ("period_change", {**periods, "dimension": "channel"}),
                 ("segment_comparison", {"group_column": "channel", "baseline_group": "线上", "current_group": "线下"})]
        for kind, values in cases:
            with self.subTest(kind=kind):
                ctx = validate_context(context(kind=kind, **values), SCHEMA)
                for item in build_pipeline(ctx, 1):
                    if item["action"] == "sql":
                        validate_read_only_sql(item["sql"])
        validate_read_only_sql("SELECT pg_catalog.sum(amount), ROUND(AVG(amount)::numeric, 2), "
                               "COALESCE(MAX(amount), 0) FROM public.sales")

    def test_table_correction_replaces_rejected_source_and_preserves_original_goal(self):
        agent, _, _, *_ = make_agent([], schema=SCHEMA)
        question = "查询 public.other 的金额合计"
        clarification = agent.run(question, source_table="public.sales")
        for original, correction in ((question, "使用 public.sales"), (question, "使用 sales"),
                                     ("查询 other 的金额合计", "使用 public.sales")):
            with self.subTest(original=original, correction=correction):
                agent, llm, sql, *_ = make_agent([
                    decision("finish", analysis_plan=plan(analysis_context=context())),
                    decision("finish", content="合计100"),
                ], schema=SCHEMA)
                result = agent.run(correction, source_table="public.sales", conversation_history=[
                    ConversationTurn(question=original, answer=clarification.answer)])
                self.assertTrue(result.finished)
                self.assertNotIn("public.other", llm.decide.call_args_list[0].args[0].question)
                self.assertIn("金额合计", llm.decide.call_args_list[0].args[0].question)
                sql.run.assert_called_once()
        agent, llm, sql, *_ = make_agent([], schema=SCHEMA)
        result = agent.run("使用 public.other", source_table="public.sales", conversation_history=[
            ConversationTurn(question=question, answer=clarification.answer)])
        self.assertTrue(result.requires_clarification)
        llm.decide.assert_not_called()
        sql.run.assert_not_called()

    def test_field_correction_does_not_reintroduce_absent_field_or_old_fallback(self):
        schema = "public.sales: 销售额 (numeric), 订单日期 (date)"
        question = "查询2026年1月的退款金额合计。如果没有退款金额字段，请明确说明，不要用销售额代替。"
        agent, _, _, *_ = make_agent([], schema=schema)
        clarification = agent.run(question, source_table="public.sales")
        agent, llm, sql, *_ = make_agent([
            decision("finish", analysis_plan=plan(analysis_context=context(metric_expression="销售额",
                time_column="订单日期", current=DateRange(start="2026-01-01", end="2026-02-01")))),
            decision("finish", content="合计100"),
        ], schema=schema)
        result = agent.run("那就用销售额", source_table="public.sales", conversation_history=[
            ConversationTurn(question=question, answer=clarification.answer)])
        self.assertTrue(result.finished)
        current_question = llm.decide.call_args_list[0].args[0].question
        self.assertIn("2026年1月", current_question)
        self.assertNotIn("退款金额", current_question)
        self.assertNotIn("不要用销售额代替", current_question)
        sql.run.assert_called_once()
        self.assertIn("2026-01-01", sql.run.call_args.args[0])
