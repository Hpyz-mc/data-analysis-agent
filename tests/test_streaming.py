import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from fastapi import HTTPException
from app.api.analysis import _analyze
from app.schemas.analysis import AnalysisRequest
from tests.test_sessions import MemoryStore, analysis_response

from app.agent.state import AgentState
from app.api.streaming import analysis_events
from app.llm.deepseek import DeepSeekClient, LLMError
from app.schemas.analysis import AnalysisResponse


def chunk(text=None, finish=None, usage=None):
    return SimpleNamespace(choices=[] if usage else [SimpleNamespace(
        delta=SimpleNamespace(content=text), finish_reason=finish)], usage=usage)


class StreamingTests(unittest.TestCase):
    def test_new_session_is_available_before_model_failure(self):
        store, events = MemoryStore(), []
        with patch("app.api.analysis.get_settings", return_value=SimpleNamespace(session_database_url="test")), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", side_effect=LLMError("模拟超时")
        ):
            with self.assertRaises(HTTPException):
                _analyze(AnalysisRequest(question="测试失败保留问题"), lambda event, data: events.append((event, data)))
        self.assertEqual(events[0][0], "session")
        record = store.turns[UUID(events[0][1]["session_id"])][0]
        self.assertEqual(record["question"], "测试失败保留问题")
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["answer"])

    def test_session_event_precedes_persisted_result(self):
        store = MemoryStore()
        with patch("app.api.analysis.get_settings", return_value=SimpleNamespace(session_database_url="test")), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", return_value=analysis_response()
        ):
            events = list(analysis_events(lambda emit: _analyze(AnalysisRequest(question="测试完整保存"), emit)))
        self.assertEqual([event.splitlines()[0] for event in events], ["event: session", "event: result"])
        result = json.loads(events[-1].splitlines()[1][6:])
        self.assertEqual(store.turns[UUID(result["session_id"])][0]["status"], "completed")

    def client(self, chunks):
        client = object.__new__(DeepSeekClient)
        client.model, client.thinking, client.usage_calls = "test", "disabled", []
        client.client = MagicMock()
        client.client.chat.completions.create.return_value = MagicMock(__iter__=lambda _: iter(chunks))
        return client

    def test_real_deltas_and_final_usage(self):
        client = self.client([chunk("核对"), chunk("完成"), chunk(finish="stop"),
                              chunk(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                                                         total_tokens=110, prompt_cache_hit_tokens=50))])
        events = []
        text = client.stream_report(AgentState(question="测试"), 7, "schema",
                                    lambda event, data: events.append((event, data)))
        self.assertEqual(text, "核对完成")
        self.assertEqual(len(events), 2)
        self.assertEqual(client.usage_snapshot().total_tokens, 110)
        client.client.chat.completions.create.return_value.close.assert_called_once()

    def test_interrupted_stream_is_not_success(self):
        client = self.client([chunk("部分回答")])
        with self.assertRaises(LLMError):
            client.stream_report(AgentState(question="测试"), 7, "schema", lambda *args: None)
        self.assertFalse(client.usage_snapshot().complete)
        self.assertEqual(client.usage_calls[0].status, "stream_error")

    def test_output_limit_retains_terminal_usage_without_retry(self):
        client = self.client([chunk("半截报告"), chunk(finish="length"),
                              chunk(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=1800,
                                                         total_tokens=1900))])
        with self.assertRaisesRegex(LLMError, "输出长度上限"):
            client.stream_report(AgentState(question="测试"), 7, "schema", lambda *args: None)
        self.assertEqual(client.usage_calls[0].finish_reason, "length")
        self.assertEqual(client.usage_calls[0].status, "output_limit")
        self.assertEqual(client.usage_snapshot().total_tokens, 1900)
        client.client.chat.completions.create.assert_called_once()
        client.client.chat.completions.create.return_value.close.assert_called_once()

    def test_sse_result_follows_worker_completion(self):
        def work(emit):
            emit("status", {"message": "查询中"})
            emit("delta", {"text": "中文\n回答"})
            return AnalysisResponse(answer="中文\n回答", rounds=1, finished=True, steps=[])
        events = list(analysis_events(work))
        self.assertEqual([e.splitlines()[0] for e in events],
                         ["event: status", "event: delta", "event: result"])
        self.assertEqual(json.loads(events[-1].splitlines()[1][6:])["answer"], "中文\n回答")
