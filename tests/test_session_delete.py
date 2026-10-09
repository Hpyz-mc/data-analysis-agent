import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.session.store import SESSION_GUARD_KEY, SessionBusyError, SessionNotFoundError, SessionStore


class SessionDeleteTests(unittest.TestCase):
    def test_store_deletes_turns_before_session_in_one_transaction(self):
        session_id = uuid4()
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [{"acquired": True}, {"acquired": True}, {"id": session_id}]
        store = SessionStore("postgresql://unused")
        with patch.object(store, "_connect", return_value=connection):
            store.delete_session(session_id)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("pg_try_advisory_lock_shared", statements[0])
        self.assertIn("pg_try_advisory_lock", statements[1])
        self.assertIn("FOR UPDATE", statements[2])
        self.assertIn("DELETE FROM public.analysis_turns", statements[3])
        self.assertIn("DELETE FROM public.analysis_sessions", statements[4])
        connection.transaction.assert_called_once()
        connection.close.assert_called_once()

    def test_store_does_not_delete_missing_or_busy_session(self):
        for first_row, second_row, error in (
            ({"acquired": False}, None, SessionBusyError),
            ({"acquired": True}, None, SessionNotFoundError),
        ):
            with self.subTest(error=error):
                connection = MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.side_effect = [{"acquired": True}, first_row, second_row]
                store = SessionStore("postgresql://unused")
                with patch.object(store, "_connect", return_value=connection):
                    with self.assertRaises(error):
                        store.delete_session(uuid4())
                self.assertFalse(any(
                    "DELETE FROM" in call.args[0] for call in cursor.execute.call_args_list
                ))
                connection.close.assert_called_once()

    def test_clear_all_uses_exclusive_guard_and_does_not_delete_when_busy(self):
        for acquired in (False, True):
            with self.subTest(acquired=acquired):
                connection = MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = {"acquired": acquired}
                store = SessionStore("postgresql://unused")
                with patch.object(store, "_connect", return_value=connection):
                    if acquired:
                        store.delete_all_sessions()
                    else:
                        with self.assertRaises(SessionBusyError):
                            store.delete_all_sessions()
                first = cursor.execute.call_args_list[0]
                self.assertEqual(first.args, (
                    "SELECT pg_try_advisory_lock(%s, %s) AS acquired", SESSION_GUARD_KEY))
                self.assertEqual(any("DELETE FROM" in call.args[0]
                                     for call in cursor.execute.call_args_list), acquired)
                connection.close.assert_called_once()

    def test_new_analysis_rejects_clear_in_progress_before_any_insert(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"acquired": False}
        store = SessionStore("postgresql://unused")
        with patch.object(store, "_connect", return_value=connection):
            with self.assertRaises(SessionBusyError):
                with store.begin_turn(None, "查询金额"):
                    self.fail("busy guard must prevent analysis")
        cursor.execute.assert_called_once_with(
            "SELECT pg_try_advisory_lock_shared(%s, %s) AS acquired", SESSION_GUARD_KEY)
        connection.close.assert_called_once()

    def test_clear_endpoint_returns_conflict_for_running_analysis(self):
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        store = MagicMock()
        store.delete_all_sessions.side_effect = SessionBusyError("有分析正在进行")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ):
            response = TestClient(app).delete("/api/v1/sessions")
        self.assertEqual(response.status_code, 409)

    def test_endpoint_deletes_requested_session(self):
        session_id = uuid4()
        store = MagicMock()
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        with patch("app.api.analysis.get_settings", return_value=settings), patch(
            "app.api.analysis.SessionStore", return_value=store
        ):
            response = TestClient(app).delete(f"/api/v1/sessions/{session_id}")
        self.assertEqual(response.status_code, 204)
        store.delete_session.assert_called_once_with(session_id)

    def test_endpoint_reports_missing_and_busy_session(self):
        session_id = uuid4()
        settings = SimpleNamespace(session_database_url="postgresql://configured")
        for error, expected in (
            (SessionNotFoundError("missing"), 404),
            (SessionBusyError("busy"), 409),
        ):
            with self.subTest(error=error):
                store = MagicMock()
                store.delete_session.side_effect = error
                with patch("app.api.analysis.get_settings", return_value=settings), patch(
                    "app.api.analysis.SessionStore", return_value=store
                ):
                    response = TestClient(app).delete(f"/api/v1/sessions/{session_id}")
                self.assertEqual(response.status_code, expected)


if __name__ == "__main__":
    unittest.main()
