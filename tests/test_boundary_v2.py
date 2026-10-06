"""Deterministic V2 boundary checks; no live database or API credentials needed."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.agent.loop import DataAnalysisAgent, TRUNCATED_RESULT_MESSAGE
from app.agent.state import AgentState, AgentStep, AnalysisPlan
from app.api.analysis import analyze
from app.llm.deepseek import DeepSeekClient, LLMError
from app.schemas.analysis import AnalysisRequest
from app.tools.chart_tool import ChartRequest, ChartTool, ChartToolError
from app.tools.python_tool import PythonAnalysisRequest, PythonAnalysisTool, PythonToolError
from app.tools.sql_tool import SQLTool, SQLToolError


def sql_step(round_number: int, rows: list[dict], truncated: bool = False) -> AgentStep:
    return AgentStep(
        round=round_number,
        action="sql",
        reasoning="边界测试",
        tool_input="SELECT id FROM public.agent_sales_sample",
        observation=json.dumps(
            {"rows": rows, "row_count": len(rows), "truncated": truncated},
            ensure_ascii=False,
        ),
    )


def plan(python_operation: str | None = None) -> AnalysisPlan:
    return AnalysisPlan(
        question_type="lookup",
        requires_sql=True,
        metric="数量",
        time_range="未指定",
        comparison="不适用",
        analysis_steps=["查询并回答"],
        python_operation=python_operation,
    )


class BoundaryTests(unittest.TestCase):
    def _sql_result(self, returned_rows: list[dict]) -> dict:
        tool = object.__new__(SQLTool)
        tool.max_rows = 100
        tool.timeout_ms = 5000
        cursor = MagicMock()
        cursor.fetchmany.return_value = returned_rows
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch.object(tool, "_connect", return_value=connection):
            result = json.loads(tool.run("SELECT id FROM public.agent_sales_sample"))
        cursor.fetchmany.assert_called_once_with(101)
        return result

    def test_01_exactly_100_rows_are_complete(self):
        result = self._sql_result([{"id": i} for i in range(100)])
        self.assertEqual(result["row_count"], 100)
        self.assertFalse(result["truncated"])

    def test_02_101_rows_are_truncated(self):
        result = self._sql_result([{"id": i} for i in range(101)])
        self.assertEqual(result["row_count"], 100)
        self.assertTrue(result["truncated"])

    def test_03_python_rejects_truncated_rows(self):
        request = PythonAnalysisRequest(
            operation="summary", source_round=1, value_column="amount"
        )
        with self.assertRaisesRegex(PythonToolError, "截断"):
            PythonAnalysisTool().run(request, [sql_step(1, [{"amount": 1}], True)])

    def test_04_chart_rejects_truncated_rows(self):
        request = ChartRequest(
            chart_type="bar", source_round=1, x_column="id", y_column="amount"
        )
        with self.assertRaisesRegex(ChartToolError, "完整数据"):
            ChartTool().run(request, [sql_step(1, [{"id": 1, "amount": 1}], True)])

    def test_05_agent_finish_guard_blocks_truncated_rows(self):
        state = AgentState(question="请汇总", plan=plan(), steps=[sql_step(1, [{"id": 1}], True)])
        self.assertEqual(DataAnalysisAgent._finish_feedback(state), TRUNCATED_RESULT_MESSAGE)

    def test_05b_agent_immediately_refuses_truncated_sql(self):
        decision = SimpleNamespace(
            action="sql", plan=plan(), sql="SELECT id FROM public.agent_sales_sample",
            reasoning="查询", content="", python=None, chart=None,
        )
        llm = SimpleNamespace(decide=MagicMock(return_value=decision))
        sql = SimpleNamespace(
            get_schema=MagicMock(return_value="public.agent_sales_sample: id (integer)"),
            run=MagicMock(return_value=json.dumps({
                "rows": [{"id": i} for i in range(100)],
                "row_count": 100,
                "truncated": True,
            })),
        )
        agent = DataAnalysisAgent(llm, sql, PythonAnalysisTool(), ChartTool(), max_rounds=3)
        result = agent.run("请汇总全部明细")
        self.assertEqual(result.answer, TRUNCATED_RESULT_MESSAGE)
        self.assertFalse(result.finished)
        self.assertEqual(result.rounds, 1)
        self.assertEqual(len(result.steps), 1)
        llm.decide.assert_called_once()

    def test_05c_agent_can_finish_with_exactly_100_complete_rows(self):
        state = AgentState(
            question="请汇总", plan=plan(),
            steps=[sql_step(1, [{"id": i} for i in range(100)])],
        )
        self.assertEqual(DataAnalysisAgent._finish_feedback(state), "")

    def test_06_python_summary_uses_complete_rows(self):
        request = PythonAnalysisRequest(
            operation="summary", source_round=1, value_column="amount"
        )
        result = json.loads(
            PythonAnalysisTool().run(
                request, [sql_step(1, [{"amount": "10"}, {"amount": "20"}])]
            )
        )
        self.assertEqual(result["result"]["sum"], "30")

    def test_07_malformed_llm_decision_is_rejected(self):
        client = object.__new__(DeepSeekClient)
        client.model = "test"
        client.thinking = "disabled"
        client.client = MagicMock()
        client.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"action":"finish"}'))]
        )
        with self.assertRaisesRegex(LLMError, "决策格式"):
            client.decide(AgentState(question="测试"), 7, "public.sample: id (integer)")

    def test_08_api_maps_llm_error_to_502(self):
        with patch("app.api.analysis.get_settings", return_value=SimpleNamespace(session_database_url=None)), patch(
            "app.api.analysis.DeepSeekClient"
        ), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent"
        ) as agent_type:
            agent_type.return_value.run.side_effect = LLMError("决策格式错误")
            with self.assertRaises(HTTPException) as caught:
                analyze(AnalysisRequest(question="测试问题"))
        self.assertEqual(caught.exception.status_code, 502)

    def test_09_sql_error_is_not_treated_as_data(self):
        state = AgentState(
            question="请汇总",
            plan=plan(),
            steps=[AgentStep(
                round=1,
                action="sql",
                reasoning="边界测试",
                tool_input="SELECT 1",
                observation="工具错误：SQL 执行失败：statement timeout",
            )],
        )
        self.assertFalse(DataAnalysisAgent._has_query_result(state.steps[0]))

    def test_10_second_round_can_finish_after_valid_sql(self):
        decisions = [
            SimpleNamespace(
                action="sql", plan=plan(), sql="SELECT COUNT(*) AS count FROM public.agent_sales_sample",
                reasoning="查询", content="", python=None, chart=None,
            ),
            SimpleNamespace(
                action="finish", plan=None, sql=None,
                reasoning="完成", content="共有 9 条。", python=None, chart=None,
            ),
        ]
        llm = SimpleNamespace(decide=MagicMock(side_effect=decisions))
        sql = SimpleNamespace(
            get_schema=MagicMock(return_value="public.agent_sales_sample: id (integer)"),
            run=MagicMock(return_value=json.dumps({
                "rows": [{"count": 9}], "row_count": 1, "truncated": False,
            })),
        )
        agent = DataAnalysisAgent(llm, sql, PythonAnalysisTool(), ChartTool(), max_rounds=3)
        result = agent.run("一共有多少条？")
        self.assertTrue(result.finished)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.answer, "共有 9 条。")


if __name__ == "__main__":
    unittest.main()
