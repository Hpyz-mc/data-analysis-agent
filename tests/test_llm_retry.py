"""Retry only invalid model decisions, without replaying tools."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from openai import APIConnectionError

from app.agent.loop import DataAnalysisAgent
from app.agent.state import AgentState
from app.llm.deepseek import DeepSeekClient, LLMError
from app.main import app
from app.tools.chart_tool import ChartTool
from app.tools.python_tool import PythonAnalysisTool


def model_response(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


PLAN = {
    "question_type": "lookup",
    "requires_sql": True,
    "metric": "记录数",
    "time_range": "未指定",
    "comparison": "不适用",
    "dimensions": [],
    "analysis_steps": ["查询记录数"],
    "python_operation": None,
    "chart_type": None,
}


def decision_json(action: str, plan: dict | None = None) -> str:
    return json.dumps(
        {
            "action": action,
            "reasoning": "测试",
            "plan": plan,
            "sql": "SELECT COUNT(*) AS count FROM public.agent_sales_sample"
            if action == "sql" else None,
            "python": None,
            "chart": None,
            "content": "共有 9 条。" if action == "finish" else "",
        },
        ensure_ascii=False,
    )


class LLMRetryTests(unittest.TestCase):
    def make_client(self, responses) -> DeepSeekClient:
        client = object.__new__(DeepSeekClient)
        client.model = "test"
        client.thinking = "disabled"
        client.client = MagicMock()
        client.client.chat.completions.create.side_effect = responses
        return client

    def test_valid_decision_does_not_retry(self):
        client = self.make_client([model_response(decision_json("sql", PLAN))])
        result = client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(result.action, "sql")
        self.assertEqual(client.client.chat.completions.create.call_count, 1)

    def test_invalid_decision_retries_once_and_recovers(self):
        client = self.make_client([
            model_response('{"action":"finish"}'),
            model_response(decision_json("sql", PLAN)),
        ])
        with patch("app.llm.deepseek.logger") as logger:
            result = client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(result.action, "sql")
        self.assertEqual(client.client.chat.completions.create.call_count, 2)
        calls = client.client.chat.completions.create.call_args_list
        self.assertEqual(len(calls[0].kwargs["messages"]), 2)
        self.assertEqual(len(calls[1].kwargs["messages"]), 3)
        logger.warning.assert_called_once()

    def test_empty_content_retries_once(self):
        client = self.make_client([
            model_response(None),
            model_response(decision_json("sql", PLAN)),
        ])
        result = client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(result.action, "sql")
        self.assertEqual(client.client.chat.completions.create.call_count, 2)

    def test_two_invalid_decisions_raise_clear_error(self):
        client = self.make_client([
            model_response('{"action":"finish"}'),
            model_response('{"action":"finish"}'),
        ])
        with self.assertRaisesRegex(LLMError, "连续两次"):
            client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(client.client.chat.completions.create.call_count, 2)

    def test_connection_error_is_not_retried_by_decision_layer(self):
        error = APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
        client = self.make_client([error])
        with self.assertRaisesRegex(LLMError, "无法连接"):
            client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(client.client.chat.completions.create.call_count, 1)

    def test_retried_decision_runs_sql_only_once(self):
        client = self.make_client([
            model_response('{"action":"sql"}'),
            model_response(decision_json("sql", PLAN)),
            model_response(decision_json("finish")),
        ])
        sql = SimpleNamespace(
            get_schema=MagicMock(return_value="public.agent_sales_sample: id (integer)"),
            run=MagicMock(return_value=json.dumps({
                "rows": [{"count": 9}], "row_count": 1, "truncated": False,
            })),
        )
        agent = DataAnalysisAgent(client, sql, PythonAnalysisTool(), ChartTool(), max_rounds=3)
        result = agent.run("一共有多少条？")
        self.assertTrue(result.finished)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(len(result.steps), 1)
        sql.run.assert_called_once()
        self.assertEqual(client.client.chat.completions.create.call_count, 3)

    def test_two_invalid_decisions_return_502_with_request_id(self):
        client = self.make_client([
            model_response('{"action":"finish"}'),
            model_response('{"action":"finish"}'),
        ])
        sql = SimpleNamespace(
            get_schema=MagicMock(return_value="public.agent_sales_sample: id (integer)"),
            run=MagicMock(),
        )
        with patch("app.api.analysis.get_settings", return_value=SimpleNamespace(session_database_url=None)), patch(
            "app.api.analysis.DeepSeekClient", return_value=client
        ), patch(
            "app.api.analysis.SQLTool", return_value=sql
        ), patch("app.llm.deepseek.logger"):
            response = TestClient(app).post(
                "/api/v1/analyze", json={"question": "请查询总额"}
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("连续两次", response.json()["detail"])
        self.assertEqual(len(response.headers["X-Request-ID"]), 32)
        sql.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
