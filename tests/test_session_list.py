import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.session.store import SessionStore, _display_question


class SessionListTests(unittest.TestCase):
    def test_display_question_removes_only_the_generated_source_prefix(self):
        self.assertEqual(
            _display_question("使用数据表 public.user_sales_20261003_01。查询月度销售额"),
            "查询月度销售额",
        )
        self.assertEqual(_display_question("请比较 public.sales 和其他表"), "请比较 public.sales 和其他表")
        self.assertEqual(_display_question("使用数据表 public.sales。"), "使用数据表 public.sales。")

    def test_store_titles_are_first_fifteen_characters(self):
        session_id = uuid4()
        created = datetime.now(timezone.utc)
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{
            "session_id": session_id,
            "first_question": "查询今年每个月销售额并画折线图",
            "created_at": created,
            "updated_at": created,
        }]
        store = SessionStore("postgresql://unused")
        with patch.object(store, "_connect", return_value=connection):
            sessions = store.list_sessions(limit=20)
        self.assertEqual(sessions[0]["title"], "查询今年每个月销售额并画折线图"[:15])
        self.assertEqual(sessions[0]["question_preview"], "查询今年每个月销售额并画折线图")
        sql, params = cursor.execute.call_args.args
        self.assertIn("ORDER BY s.updated_at DESC", sql)
        self.assertIn("ORDER BY turn_no ASC", sql)
        self.assertEqual(params, (20,))
        connection.close.assert_called_once()

    def test_existing_prefixed_questions_get_clean_titles_without_writes(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        question = "查询 2026 年 1 至 3 月每月销售额并画折线图。"
        cursor.fetchall.return_value = [{
            "session_id": uuid4(), "first_question": "使用数据表 public.demo_ecommerce_orders。" + question,
            "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        }]
        store = SessionStore("postgresql://unused")
        with patch.object(store, "_connect", return_value=connection):
            result = store.list_sessions()
        self.assertEqual(result[0]["title"], question[:15])
        self.assertEqual(result[0]["question_preview"], question)
        cursor.execute.assert_called_once()
        self.assertTrue(cursor.execute.call_args.args[0].strip().startswith("SELECT"))

    def test_endpoint_returns_recent_sessions(self):
        session_id = uuid4()
        created = datetime.now(timezone.utc)
        store = MagicMock()
        store.list_sessions.return_value = [{
            "session_id": session_id,
            "title": "第一轮的问题",
            "created_at": created,
            "updated_at": created,
        }]
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ):
            response = TestClient(app).get("/api/v1/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sessions"][0]["session_id"], str(session_id))
        store.list_sessions.assert_called_once_with(limit=100)

    def test_endpoint_requires_session_storage(self):
        settings = SimpleNamespace(session_database_url=None)
        with patch("app.api.analysis.get_settings", return_value=settings):
            response = TestClient(app).get("/api/v1/sessions")
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
