"""Focused checks for scoped drill-down, persistence and chart evidence."""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path
from uuid import uuid4

from app.agent.context import AnalysisContext, ScopeFilter, merge_context
from app.agent.loop import DataAnalysisAgent
from app.agent.scoped_analysis import ScopeError, build_pipeline, missing_requested_dimension, validate_context
from app.agent.state import AnalysisPlan
from app.session.store import ActiveTurn, SessionStore
from app.tools.chart_tool import ChartTool
from app.tools.python_tool import PythonAnalysisTool
from app.presentation import history_messages


SCHEMA = ("public.demo_ecommerce_orders: order_date (date), channel (text), "
          "category (text), sales_amount (numeric), stockout_days (integer)")


def scope(**changes):
    values = dict(
        kind="period_change", source_table="public.demo_ecommerce_orders",
        metric_label="销售额", metric_expression="sales_amount", time_column="order_date",
        baseline={"start": "2026-01-01", "end": "2026-02-01"},
        current={"start": "2026-02-01", "end": "2026-03-01"}, dimension="channel",
    )
    values.update(changes)
    return AnalysisContext(**values)


class AnalysisContextTests(unittest.TestCase):
    def test_followup_inherits_scope_and_uses_matching_queries(self):
        previous = scope(filters=[ScopeFilter(column="channel", values=["线下门店"])])
        proposal = scope(dimension="category", baseline={"start": "2025-01-01", "end": "2025-02-01"})
        merged = validate_context(merge_context(proposal, previous, "继续按品类下钻"), SCHEMA)
        self.assertEqual(merged.baseline, previous.baseline)
        self.assertEqual(merged.filters, previous.filters)
        pipeline = build_pipeline(merged, 1)
        total, detail = pipeline[0]["sql"], pipeline[1]["sql"]
        self.assertEqual(total.split(" WHERE ")[1].split(" GROUP BY ")[0],
                         detail.split(" WHERE ")[1].split(" GROUP BY ")[0])
        self.assertIn("'线下门店'", total)
        self.assertIn('"category" AS dimension', detail)
        reset = merge_context(scope(dimension="category"), previous, "取消筛选回到总体")
        self.assertEqual(reset.filters, [])
        changed = scope(dimension="category", baseline={"start": "2026-02-01", "end": "2026-03-01"},
                        current={"start": "2026-03-01", "end": "2026-04-01"})
        updated = merge_context(changed, previous, "那改为3月比2月，按品类归因")
        self.assertEqual(updated.baseline, changed.baseline)
        self.assertEqual(updated.filters, previous.filters)
        self.assertIn("没有商品字段", missing_requested_dimension("继续按商品下钻", previous, SCHEMA))
        lookup = merge_context(scope(kind="lookup", dimension=None), previous, "那这两个月合计多少？")
        self.assertEqual(lookup.current.start, previous.baseline.start)
        self.assertEqual(lookup.current.end, previous.current.end)
        self.assertEqual(len(build_pipeline(validate_context(lookup, SCHEMA), 1)), 1)

    def test_scope_rejects_missing_dimensions_and_unsafe_metrics(self):
        for proposal in (scope(dimension="product"), scope(metric_expression="(SELECT 1)"),
                         scope(metric_expression="stockout_days")):
            with self.subTest(proposal=proposal):
                with self.assertRaises(ScopeError):
                    validate_context(proposal, SCHEMA)

    def test_scoped_graph_runs_real_attribution_and_chart_before_report(self):
        context = scope(dimension="category", filters=[ScopeFilter(column="channel", values=["线下门店"])])
        labels = (context.baseline.label(), context.current.label())
        totals = [{"period": labels[0], "amount": "200000"}, {"period": labels[1], "amount": "163000"}]
        detail = [{"period": label, "dimension": category, "amount": amount}
                  for label, amounts in zip(labels, (("110000", "60000", "30000"), ("89650", "48900", "24450")))
                  for category, amount in zip(("电脑", "家电", "服饰"), amounts)]
        plan = AnalysisPlan(question_type="diagnosis", requires_sql=True, metric="销售额",
                            time_range="1月和2月", comparison="2月比1月", analysis_steps=["归因"],
                            analysis_context=context)
        llm = SimpleNamespace(decide=MagicMock(side_effect=[
            SimpleNamespace(plan=plan, action="finish", content="", reasoning="规划"),
            SimpleNamespace(plan=None, action="finish", content="线下下降37000，主要来自电脑。", reasoning="报告"),
        ]))
        sql = SimpleNamespace(get_schema=lambda: SCHEMA, run=MagicMock(side_effect=[
            json.dumps({"rows": totals, "truncated": False}),
            json.dumps({"rows": detail, "truncated": False}),
        ]))
        with tempfile.TemporaryDirectory() as directory, patch("app.tools.chart_tool.CHART_DIR", Path(directory)):
            agent = DataAnalysisAgent(llm, sql, PythonAnalysisTool(), ChartTool())
            result = agent.run("继续看线下渠道，按品类下钻", analysis_context=scope())
            self.assertTrue(result.finished)
            self.assertEqual([step.action for step in result.steps], ["sql", "sql", "python", "chart"])
            evidence = json.loads(result.steps[2].observation)["result"]
            self.assertEqual(evidence["total_change"], "-37000")
            self.assertEqual(len(result.charts), 1)
            self.assertEqual(result.analysis_context.filters[0].values, ["线下门店"])
            self.assertEqual(llm.decide.call_count, 2)
            chart = json.loads(result.steps[3].observation)
            self.assertEqual(chart["source_type"], "attribution")
            self.assertIn("线下门店", chart["title"])

    def test_context_and_charts_are_stored_and_restored_as_artifacts(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        context = scope()
        artifacts = {"charts": ["/api/v1/charts/stored.png"],
                     "analysis_context": context.model_dump(mode="json"),
                     "suggestions": [{"label": "继续", "question": "继续下钻"}]}
        turn = ActiveTurn(connection, uuid4(), 1, [])
        turn.finish("报告没有图表链接", "completed", artifacts=artifacts, analysis_context=context)
        updates = cursor.execute.call_args_list
        self.assertEqual(updates[0].args[1][2].obj, artifacts)
        self.assertEqual(updates[2].args[1][0].obj, context.model_dump(mode="json"))
        row = SessionStore._history_artifacts({"question": "分析", "answer": "报告没有图表链接",
                                             "status": "completed", "artifacts": artifacts})
        messages = history_messages([row])
        self.assertEqual(messages[-1]["charts"], artifacts["charts"])
        self.assertEqual(messages[-1]["analysis_context"], artifacts["analysis_context"])


if __name__ == "__main__":
    unittest.main()
