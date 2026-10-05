"""Focused regressions for trend + breakdown, completeness and scope continuity."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agent.analysis_policy import build_analysis_task
from app.agent.loop import DataAnalysisAgent
from app.agent.scoped_analysis import align_breakdown_intent, build_pipeline, validate_context
from app.agent.state import AnalysisPlan
from app.api.analysis import _analyze, _usable_for_history
from app.api.streaming import analysis_events
from app.llm.deepseek import LLMError
from app.schemas.analysis import AnalysisRequest
from tests.test_sessions import MemoryStore
from uuid import UUID
from app.tools.chart_tool import ChartTool
from app.tools.python_tool import PythonAnalysisRequest, PythonAnalysisTool, PythonToolError
from tests.test_analysis_context import SCHEMA, scope


TOTALS = [{"period": period, "amount": amount} for period, amount in
          (("2026-01", "666000"), ("2026-02", "652300"), ("2026-03", "702600"))]
DETAILS = [{"period": period, "dimension": channel, "amount": amount}
           for period, amounts in zip(("2026-01", "2026-02", "2026-03"),
                                     (("200000", "306000", "160000"),
                                      ("163000", "321300", "168000"),
                                      ("190000", "336600", "176000")))
           for channel, amount in zip(("线下门店", "线上商城", "第三方平台"), amounts)]


class TrendBreakdownTests(unittest.TestCase):
    def run_graph(self, detail=DETAILS, *, stream=False, report_error=None):
        # Deliberately omit the requested dimension from the model's plan.
        context = scope(kind="trend", baseline=None, dimension=None,
                        current={"start": "2026-01-01", "end": "2026-04-01"})
        plan = AnalysisPlan(question_type="comparison", requires_sql=True, metric="销售额",
                            time_range="2026年一季度", comparison="相邻月份", analysis_steps=["月度走势"],
                            analysis_context=context)
        llm = SimpleNamespace(decide=MagicMock(side_effect=[
            SimpleNamespace(plan=plan, action="finish", content="", reasoning="规划"),
            report_error or SimpleNamespace(plan=None, action="finish", content="总体走势和各期渠道贡献已核对。", reasoning="报告"),
        ]), stream_report=MagicMock(return_value="总体走势和各期渠道贡献已核对。", side_effect=report_error))
        sql = SimpleNamespace(get_schema=lambda: SCHEMA, run=MagicMock(side_effect=[
            json.dumps({"rows": TOTALS, "truncated": False}),
            json.dumps({"rows": detail, "truncated": False}),
        ]))
        with tempfile.TemporaryDirectory() as directory, patch("app.tools.chart_tool.CHART_DIR", Path(directory)):
            result = DataAnalysisAgent(llm, sql, PythonAnalysisTool(), ChartTool()).run(
                "析销售额的月度变化，并按销售渠道拆解主要变化来源，画趋势图和贡献图。",
                emit=MagicMock() if stream else None,
            )
        return result, llm, sql

    def test_compound_request_executes_both_goals_with_two_queries(self):
        result, llm, sql = self.run_graph()
        self.assertTrue(result.finished)
        self.assertEqual(sql.run.call_count, 2)
        self.assertEqual(llm.decide.call_count, 2)
        self.assertEqual(result.analysis_context.dimension, "channel")
        self.assertEqual(len(result.charts), 2)
        evidence = json.loads(result.steps[4].observation)["result"]
        self.assertEqual(len(evidence["comparisons"]), 2)
        self.assertEqual([pair["total_change"] for pair in evidence["comparisons"]], ["-13700", "50300"])
        self.assertEqual(evidence["comparisons"][0]["details"][0]["change"], "-37000")
        self.assertEqual(evidence["comparisons"][0]["details"][0]["contribution_percent"], "270.07")
        self.assertEqual(evidence["comparisons"][1]["details"][0]["change"], "27000")
        self.assertTrue(_usable_for_history(result))

    def test_failed_reconciliation_is_partial_and_not_streamed_as_success(self):
        bad = [dict(row) for row in DETAILS]
        bad[0]["amount"] = "199999"
        result, llm, _ = self.run_graph(bad, stream=True)
        self.assertFalse(result.finished)
        self.assertIn("本轮仅部分完成", result.answer)
        self.assertIn("666000", result.answer)
        self.assertIn("不一致", result.answer)
        self.assertFalse(_usable_for_history(result))
        llm.stream_report.assert_not_called()
        self.assertEqual(llm.decide.call_count, 1)

    def test_missing_or_extra_period_is_not_silently_ignored(self):
        tool = PythonAnalysisTool()
        result, _, _ = self.run_graph()
        request = PythonAnalysisRequest(operation="sequential_attribution", source_round=4,
            total_source_round=1, period_column="period", dimension_column="dimension", value_column="amount")
        for rows in (DETAILS[:6], [*DETAILS, {"period": "2026-04", "dimension": "线下门店", "amount": "1"}]):
            with self.subTest(rows=len(rows)):
                steps = [step.model_copy(deep=True) for step in result.steps]
                steps[3].observation = json.dumps({"rows": rows, "truncated": False})
                with self.assertRaisesRegex(PythonToolError, "时期不一致"):
                    tool.run(request, steps)

    def test_intent_scope_filters_and_total_reset(self):
        self.assertEqual(build_analysis_task("析销售额的月度变化，并按销售渠道拆解主要变化来源").kind, "trend")
        for filters in ([{"column": "channel", "values": ["线下门店"]}], []):
            context = scope(kind="trend", baseline=None, dimension=None, filters=filters,
                            current={"start": "2026-01-01", "end": "2026-04-01"})
            context = validate_context(align_breakdown_intent(context, "按品类拆解月度变化", SCHEMA), SCHEMA)
            pipeline = build_pipeline(context, 2)
            total, detail = pipeline[0]["sql"], pipeline[3]["sql"]
            self.assertEqual(total.split(" WHERE ")[1].split(" GROUP BY ")[0],
                             detail.split(" WHERE ")[1].split(" GROUP BY ")[0])
            self.assertIn('"category" AS dimension', detail)
            self.assertEqual("'线下门店'" in detail, bool(filters))

    def test_final_report_failure_preserves_evidence_without_repeating_tools(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                result, llm, sql = self.run_graph(stream=stream, report_error=LLMError("模拟文字报告中断"))
                self.assertFalse(result.finished)
                self.assertFalse(_usable_for_history(result))
                self.assertIsNone(result.analysis_context)
                self.assertEqual(result.suggestions, [])
                self.assertEqual(len(result.charts), 2)
                for text in ("文字报告未完成", "666000", "-37000", "50300", "270.07", "已生成的图表"):
                    self.assertIn(text, result.answer)
                self.assertEqual(sql.run.call_count, 2)
                self.assertEqual(llm.decide.call_count, 1 if stream else 2)
                self.assertEqual(llm.stream_report.call_count, 1 if stream else 0)

    def test_partial_result_is_persisted_before_sse_result(self):
        result, _, _ = self.run_graph(stream=True, report_error=LLMError("模拟输出上限"))
        store = MemoryStore()
        with patch("app.api.analysis.get_settings", return_value=SimpleNamespace(session_database_url="test")), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch("app.api.analysis.DataAnalysisAgent.run", return_value=result):
            events = list(analysis_events(lambda emit: _analyze(AnalysisRequest(question="查询趋势并拆解渠道"), emit)))
        self.assertEqual(events[-1].splitlines()[0], "event: result")
        payload = json.loads(events[-1].splitlines()[1][6:])
        record = store.turns[UUID(payload["session_id"])][0]
        self.assertFalse(payload["finished"])
        self.assertEqual(record["status"], "incomplete")
        self.assertEqual(record["answer"], result.answer)
        self.assertEqual(record["artifacts"]["charts"], result.charts)


if __name__ == "__main__":
    unittest.main()
