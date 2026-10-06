"""Ranking, distribution and decile analysis kinds."""
import json
import unittest

from app.agent.analysis_policy import build_analysis_task
from app.agent.context import AnalysisContext
from app.agent.scoped_analysis import ScopeError, build_pipeline, validate_context
from app.agent.state import AgentStep
from app.tools.python_tool import PythonAnalysisRequest, PythonAnalysisTool, PythonToolError

SCHEMA = "public.sales: amount (numeric), channel (text), order_date (date)"


def context(kind, **kw):
    values = dict(kind=kind, source_table="public.sales", metric_label="销售额",
                  metric_expression="amount", time_column="order_date", time_grain=None,
                  baseline=None, current=None, group_column=None, baseline_group=None,
                  current_group=None, dimension=None, confirmed_order_column=None,
                  limit_n=None, rank_column=None, filters=[], drill_path=[])
    values.update(kw)
    return AnalysisContext(**values)


def step(round_, rows, tool_input=""):
    return AgentStep(round=round_, action="sql", reasoning="", tool_input=tool_input,
                     observation=json.dumps({"rows": rows}, ensure_ascii=False))


class AnalysisKindsTests(unittest.TestCase):
    def test_intent_classification(self):
        self.assertEqual(build_analysis_task("销售额前 5 的品类是哪些").kind, "ranking")
        self.assertEqual(build_analysis_task("各渠道销售额占比").kind, "distribution")
        self.assertEqual(build_analysis_task("按订单金额十分位分层分析销售额").kind, "decile")
        # Existing kinds are unaffected.
        self.assertEqual(build_analysis_task("各月销售额走势").kind, "trend")

    def test_ranking_pipeline(self):
        ctx = validate_context(context("ranking", dimension="channel", limit_n=5), SCHEMA)
        pipeline = build_pipeline(ctx, 1, "bar")
        self.assertEqual([item["action"] for item in pipeline], ["sql", "chart"])
        self.assertIn("LIMIT 5", pipeline[0]["sql"])
        self.assertIn("ORDER BY amount DESC", pipeline[0]["sql"])
        self.assertEqual(pipeline[1]["chart"]["x_column"], "dimension")

    def test_distribution_pipeline(self):
        ctx = validate_context(context("distribution", dimension="channel"), SCHEMA)
        pipeline = build_pipeline(ctx, 1, "bar")
        self.assertEqual([item["action"] for item in pipeline], ["sql", "python", "chart"])
        self.assertEqual(pipeline[1]["python"]["operation"], "share")
        self.assertEqual(pipeline[1]["python"]["label_column"], "dimension")

    def test_decile_pipeline_and_reconciliation(self):
        ctx = validate_context(context("decile", rank_column="amount"), SCHEMA)
        pipeline = build_pipeline(ctx, 1, "bar")
        self.assertEqual([item["action"] for item in pipeline], ["sql", "sql", "python", "chart"])
        self.assertIn("ntile(10)", pipeline[0]["sql"])
        self.assertEqual(pipeline[2]["python"]["operation"], "decile")
        self.assertEqual(pipeline[2]["python"]["total_source_round"], 2)
        tool = PythonAnalysisTool()
        buckets = [{"bucket": i, "amount": "10", "row_count": 1} for i in range(1, 11)]
        total = [{"amount": "100"}]
        request = PythonAnalysisRequest(operation="decile", source_round=1,
                                        total_source_round=2, value_column="amount")
        result = json.loads(tool.run(request, [step(1, buckets), step(2, total)]))
        self.assertTrue(result["result"]["reconciled"])
        self.assertEqual(result["result"]["sum_of_buckets"], "100")
        self.assertEqual(result["result"]["buckets"][0]["share_percent"], "10.00")

    def test_decile_mismatch_raises(self):
        tool = PythonAnalysisTool()
        buckets = [{"bucket": i, "amount": "10", "row_count": 1} for i in range(1, 11)]
        total = [{"amount": "105"}]
        request = PythonAnalysisRequest(operation="decile", source_round=1,
                                        total_source_round=2, value_column="amount")
        with self.assertRaises(PythonToolError):
            tool.run(request, [step(1, buckets), step(2, total)])

    def test_validation_requires_dimension_or_rank_column(self):
        with self.assertRaises(ScopeError):
            validate_context(context("ranking", dimension=None), SCHEMA)
        with self.assertRaises(ScopeError):
            validate_context(context("distribution", dimension=None), SCHEMA)
        with self.assertRaises(ScopeError):
            validate_context(context("decile", rank_column=None), SCHEMA)
        with self.assertRaises(ScopeError):
            validate_context(context("decile", rank_column="channel"), SCHEMA)

    def test_correlation_pipeline(self):
        self.assertEqual(build_analysis_task("销售额和订单量相关吗").kind, "correlation")
        self.assertEqual(build_analysis_task("对订单金额做 1% 和 99% 缩尾").kind, "outlier")
        ctx = validate_context(context("correlation", correlate_column="order_count"),
                                SCHEMA + ", order_count (integer)")
        pipeline = build_pipeline(ctx, 1, "scatter")
        self.assertEqual([item["action"] for item in pipeline], ["sql", "sql", "chart"])
        self.assertIn("corr(", pipeline[0]["sql"])
        self.assertIn("ntile(30)", pipeline[1]["sql"])
        self.assertEqual(pipeline[2]["chart"]["chart_type"], "scatter")
        with self.assertRaises(ScopeError):
            validate_context(context("correlation", correlate_column=None), SCHEMA)
        with self.assertRaises(ScopeError):
            validate_context(context("correlation", correlate_column="channel"), SCHEMA)

    def test_outlier_pipeline(self):
        ctx = validate_context(context("outlier", outlier_mode="winsorize"), SCHEMA)
        pipeline = build_pipeline(ctx, 1)
        self.assertEqual([item["action"] for item in pipeline], ["sql"])
        sql = pipeline[0]["sql"]
        self.assertIn("percentile_cont(0.01)", sql)
        self.assertIn("winsorized_amount", sql)
        self.assertIn("truncated_amount", sql)

    def test_summary_pipeline(self):
        self.assertEqual(build_analysis_task("订单金额的平均值、最大值和最小值").kind, "summary")
        ctx = validate_context(context("summary"), SCHEMA)
        pipeline = build_pipeline(ctx, 1)
        self.assertEqual([item["action"] for item in pipeline], ["sql"])
        sql = pipeline[0]["sql"]
        self.assertIn("AVG(", sql)
        self.assertIn("MIN(", sql)
        self.assertIn("MAX(", sql)
        self.assertIn("SUM(", sql)

    def test_single_row_kinds_reject_charts(self):
        from app.agent.answer_contract import append_requested_charts
        for kind in ("summary", "outlier"):
            ctx = validate_context(context(kind), SCHEMA)
            pipeline = build_pipeline(ctx, 1)
            with self.assertRaises(ScopeError):
                append_requested_charts(pipeline, ctx, "画柱状图", 1)
        ctx = validate_context(context("correlation", correlate_column="order_count"),
                                SCHEMA + ", order_count (integer)")
        pipeline = build_pipeline(ctx, 1, "scatter")
        with self.assertRaises(ScopeError):
            append_requested_charts(pipeline, ctx, "画折线图", 1)


if __name__ == "__main__":
    unittest.main()
