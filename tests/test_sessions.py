"""Session behavior without live PostgreSQL credentials or DeepSeek calls."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.agent.prompts import build_user_prompt
from app.agent.state import AgentState, AgentStep, AnalysisPlan, ConversationTurn
from app.api.analysis import _usable_for_history
from app.llm.deepseek import LLMError
from app.main import app
from app.schemas.analysis import AnalysisResponse
from app.session.store import (
    SessionBusyError,
    SessionNotFoundError,
    SessionStore,
    _bounded_history,
)


def analysis_response(answer: str = "总额是 50000。", finished: bool = True) -> AnalysisResponse:
    return AnalysisResponse(
        answer=answer,
        plan=AnalysisPlan(
            question_type="lookup", requires_sql=True,
            metric="销售额", time_range="第一季度", comparison="不适用",
            analysis_steps=["查询销售额"],
        ),
        rounds=2,
        finished=finished,
        steps=[AgentStep(
            round=1, action="sql", reasoning="查询", tool_input="SELECT SUM(...) AS total",
            observation=json.dumps({
                "rows": [{"total": "50000"}], "row_count": 1, "truncated": False,
            }),
        )],
    )


class MemoryStore:
    """A small contract fake; production storage is PostgreSQL only."""

    def __init__(self):
        self.turns: dict[UUID, list[dict]] = {}

    @contextmanager
    def begin_turn(self, session_id, question):
        selected = session_id or uuid4()
        if session_id is not None and selected not in self.turns:
            raise SessionNotFoundError("找不到这段对话。")
        history = [
            ConversationTurn(question=turn["question"], answer=turn["answer"])
            for turn in self.turns.get(selected, [])
            if turn["status"] == "completed"
        ][-5:]
        record = {"question": question, "answer": None, "status": "running"}
        self.turns.setdefault(selected, []).append(record)

        def finish(answer, status, **metadata):
            record.update(answer=answer, status=status, **metadata)

        active = SimpleNamespace(session_id=selected, history=history, finish=finish)
        try:
            yield active
        finally:
            if record["status"] == "running":
                record["status"] = "failed"

    def get_history(self, session_id):
        if session_id not in self.turns:
            raise SessionNotFoundError("找不到这段对话。")
        now = datetime.now(timezone.utc)
        return [
            dict(turn_no=i, created_at=now, finished_at=now, **turn)
            for i, turn in enumerate(self.turns[session_id], 1)
        ]


class SessionTests(unittest.TestCase):
    def test_two_sessions_in_parallel_keep_histories_separate(self):
        store = MemoryStore()
        first_id, second_id = uuid4(), uuid4()
        store.turns[first_id] = [
            {"question": "甲的旧问题", "answer": "甲的旧答案", "status": "completed"}
        ]
        store.turns[second_id] = [
            {"question": "乙的旧问题", "answer": "乙的旧答案", "status": "completed"}
        ]
        barrier = Barrier(2)
        seen: dict[str, list[str]] = {}

        def run_parallel(*, question, conversation_history):
            seen[question] = [turn.answer for turn in conversation_history]
            barrier.wait(timeout=5)  # Both requests must overlap inside the Agent.
            return analysis_response(f"{question}的结果")

        def ask(session_id, question):
            return TestClient(app).post("/api/v1/analyze", json={
                "question": question, "session_id": str(session_id),
            })

        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", side_effect=run_parallel
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(ask, first_id, "甲的追问")
                second_future = pool.submit(ask, second_id, "乙的追问")
                first, second = first_future.result(), second_future.result()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["session_id"], str(first_id))
        self.assertEqual(second.json()["session_id"], str(second_id))
        self.assertEqual(seen, {"甲的追问": ["甲的旧答案"], "乙的追问": ["乙的旧答案"]})
        self.assertEqual(store.turns[first_id][-1]["answer"], "甲的追问的结果")
        self.assertEqual(store.turns[second_id][-1]["answer"], "乙的追问的结果")

    def test_history_budget_prefers_recent_completed_turns(self):
        rows = [
            {"question": "最近" * 300, "answer": "答案" * 500},
            {"question": "更早的问题", "answer": "更早的答案"},
        ]
        history = _bounded_history(rows, max_chars=1000)
        self.assertEqual(len(history), 1)
        self.assertLessEqual(len(history[0].question) + len(history[0].answer), 1000)
        self.assertTrue(history[0].question.startswith("最近"))

    def test_prompt_separates_conversation_from_current_tools(self):
        state = AgentState(
            question="那哪个月最高？",
            current_round=1,
            conversation_history=[ConversationTurn(
                question="第一季度总额？", answer="总额是 50000。"
            )],
        )
        prompt = build_user_prompt(state, 7, "public.agent_sales_sample: order_date (date)")
        self.assertIn("那哪个月最高？", prompt)
        self.assertIn("第一季度总额？", prompt)
        self.assertIn("不能当作本轮证据", prompt)
        self.assertIn("暂无工具调用记录", prompt)

    def test_incomplete_turn_is_not_context_evidence(self):
        self.assertTrue(_usable_for_history(analysis_response()))
        self.assertFalse(_usable_for_history(analysis_response(finished=False)))
        failed = analysis_response()
        failed.steps[0].observation = "工具错误：超时"
        self.assertFalse(_usable_for_history(failed))

    def test_version_clarification_is_available_to_followup(self):
        store = MemoryStore()
        clarification = "我检测到数据库中存在多份相似的数据表：\n1. user_sales_20260929_01\n2. user_sales_20260929_02\n请问您当前这轮分析需要使用哪一份数据？"
        first_response = AnalysisResponse(
            answer=clarification, plan=None, rounds=0, finished=False, steps=[], charts=[],
        )
        seen = []

        def run(*, question, conversation_history=None):
            seen.append(conversation_history or [])
            return first_response if len(seen) == 1 else analysis_response()

        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", side_effect=run
        ):
            first = TestClient(app).post("/api/v1/analyze", json={"question": "分析销售额"})
            session_id = first.json()["session_id"]
            second = TestClient(app).post("/api/v1/analyze", json={
                "question": "用 user_sales_20260929_02", "session_id": session_id,
            })

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(store.turns[UUID(session_id)][0]["status"], "completed")
        self.assertEqual(seen[1][0].question, "分析销售额")
        self.assertEqual(seen[1][0].answer, clarification)
        first_response.steps = [AgentStep(
            round=1, action="sql", reasoning="", tool_input="", observation="",
        )]
        self.assertFalse(_usable_for_history(first_response))

    def test_first_question_and_followup_share_only_own_history(self):
        store = MemoryStore()
        responses = [
            analysis_response("第一季度总额是 50000。"),
            analysis_response("3 月最高。"),
            analysis_response("另一个会话的回答。"),
        ]
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", side_effect=responses
        ) as run_mock:
            client = TestClient(app)
            first = client.post("/api/v1/analyze", json={"question": "第一季度总额？"})
            self.assertEqual(first.status_code, 200)
            session_id = first.json()["session_id"]
            second = client.post("/api/v1/analyze", json={
                "question": "那哪个月最高？", "session_id": session_id,
            })
            third = client.post("/api/v1/analyze", json={"question": "另一个问题"})
            history = client.get(f"/api/v1/sessions/{session_id}")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["session_id"], session_id)
        self.assertNotEqual(third.json()["session_id"], session_id)
        self.assertEqual(
            run_mock.call_args_list[1].kwargs["conversation_history"][0].answer,
            "第一季度总额是 50000。",
        )
        self.assertEqual(run_mock.call_args_list[2].kwargs["conversation_history"], [])
        self.assertEqual(len(history.json()["turns"]), 2)

    def test_failed_turn_is_excluded_from_next_prompt(self):
        store = MemoryStore()
        responses = [analysis_response("首轮成功"), LLMError("模型失败"), analysis_response("后续成功")]
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", side_effect=responses
        ) as run_mock:
            client = TestClient(app)
            first = client.post("/api/v1/analyze", json={"question": "第一问"})
            selected = first.json()["session_id"]
            failure = client.post("/api/v1/analyze", json={
                "question": "失败问题", "session_id": selected,
            })
            third = client.post("/api/v1/analyze", json={
                "question": "继续追问", "session_id": selected,
            })
        self.assertEqual(failure.status_code, 502)
        self.assertEqual(third.status_code, 200)
        self.assertEqual(len(run_mock.call_args_list[2].kwargs["conversation_history"]), 1)
        self.assertEqual(store.turns[UUID(selected)][1]["status"], "failed")

    def test_incomplete_turn_is_saved_but_not_used_as_followup_context(self):
        store = MemoryStore()
        responses = [
            analysis_response("数据截断，请缩小范围。", finished=False),
            analysis_response("重新查询后的答案。"),
        ]
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", side_effect=responses
        ) as run_mock:
            client = TestClient(app)
            first = client.post("/api/v1/analyze", json={"question": "返回全部明细"})
            selected = first.json()["session_id"]
            second = client.post("/api/v1/analyze", json={
                "question": "改成按月汇总", "session_id": selected,
            })
            history = client.get(f"/api/v1/sessions/{selected}")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(run_mock.call_args_list[1].kwargs["conversation_history"], [])
        self.assertEqual(history.json()["turns"][0]["status"], "incomplete")
        self.assertEqual(history.json()["turns"][1]["status"], "completed")

    def test_failed_and_truncated_turns_do_not_pollute_later_context(self):
        store = MemoryStore()
        seen: dict[str, list[str]] = {}

        def run_with_bad_turns(*, question, conversation_history):
            seen[question] = [turn.answer for turn in conversation_history]
            if question == "触发模型报错":
                raise LLMError("模拟模型报错")
            if question == "查询超过100行":
                response = analysis_response("查询数据量过大，请缩小查询范围", finished=False)
                response.steps[0].observation = json.dumps({
                    "rows": [{"total": "1"}], "row_count": 100, "truncated": True,
                })
                return response
            return analysis_response(f"{question}的可信回答")

        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch(
            "app.api.analysis.DataAnalysisAgent.run", side_effect=run_with_bad_turns
        ):
            client = TestClient(app)
            initial = client.post("/api/v1/analyze", json={"question": "正常首问"})
            selected = initial.json()["session_id"]
            failed = client.post("/api/v1/analyze", json={
                "question": "触发模型报错", "session_id": selected,
            })
            truncated = client.post("/api/v1/analyze", json={
                "question": "查询超过100行", "session_id": selected,
            })
            followup = client.post("/api/v1/analyze", json={
                "question": "最后追问", "session_id": selected,
            })
            history = client.get(f"/api/v1/sessions/{selected}")

        self.assertEqual([initial.status_code, failed.status_code,
                          truncated.status_code, followup.status_code], [200, 502, 200, 200])
        self.assertFalse(truncated.json()["finished"])
        self.assertEqual(seen["最后追问"], ["正常首问的可信回答"])
        self.assertEqual(
            [turn["status"] for turn in history.json()["turns"]],
            ["completed", "failed", "incomplete", "completed"],
        )

    def test_unknown_session_returns_404(self):
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=MemoryStore()
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch("app.api.analysis.DataAnalysisAgent"):
            response = TestClient(app).post("/api/v1/analyze", json={
                "question": "问题", "session_id": str(uuid4()),
            })
        self.assertEqual(response.status_code, 404)

    def test_busy_session_returns_409(self):
        class BusyStore:
            @contextmanager
            def begin_turn(self, session_id, question):
                raise SessionBusyError("这段对话正在处理上一条问题，请稍后再试。")
                yield

        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=BusyStore()
        ), patch("app.api.analysis.DeepSeekClient"), patch("app.api.analysis.SQLTool"), patch(
            "app.api.analysis.PythonAnalysisTool"
        ), patch("app.api.analysis.ChartTool"), patch("app.api.analysis.DataAnalysisAgent"):
            response = TestClient(app).post("/api/v1/analyze", json={"question": "问题"})
        self.assertEqual(response.status_code, 409)

    def test_unconfigured_mode_stays_stateless(self):
        settings = SimpleNamespace(session_database_url=None)
        agent = MagicMock()
        agent.run.return_value = analysis_response()
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.DeepSeekClient"
        ), patch("app.api.analysis.SQLTool"), patch("app.api.analysis.PythonAnalysisTool"), patch(
            "app.api.analysis.ChartTool"
        ), patch("app.api.analysis.DataAnalysisAgent", return_value=agent):
            response = TestClient(app).post("/api/v1/analyze", json={"question": "问题"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["session_id"])

    def test_postgres_advisory_lock_and_completion_contract(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor = MagicMock()
        connection.transaction.return_value.__enter__.return_value = MagicMock()
        cursor.fetchone.side_effect = [
            {"acquired": True}, {"acquired": True}, {"turn_no": 42},
        ]
        cursor.fetchall.return_value = []
        cursor.rowcount = 1
        with patch.object(SessionStore, "_connect", return_value=connection):
            with SessionStore("postgresql://configured").begin_turn(None, "问题") as turn:
                self.assertEqual(turn.turn_no, 42)
                self.assertEqual(turn.history, [])
                turn.finish("答案", "completed")
        self.assertTrue(connection.close.called)
        executed_sql = " ".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("pg_try_advisory_lock", executed_sql)
        self.assertIn("pg_try_advisory_lock_shared", executed_sql)
        self.assertIn("INSERT INTO public.analysis_turns", executed_sql)
        self.assertIn("status = %s", executed_sql)

    def test_history_command_can_read_more_than_api_twenty_turn_limit(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"id": uuid4()}
        cursor.fetchall.return_value = [
            {"turn_no": number, "question": f"问题{number}", "answer": f"答案{number}"}
            for number in range(1, 26)
        ]
        with patch.object(SessionStore, "_connect", return_value=connection):
            turns = SessionStore("postgresql://configured").get_history(uuid4(), limit=None)
        self.assertEqual(len(turns), 25)
        self.assertEqual(turns[0]["turn_no"], 1)
        self.assertNotIn("LIMIT", cursor.execute.call_args_list[-1].args[0])
        self.assertTrue(connection.close.called)


if __name__ == "__main__":
    unittest.main()
