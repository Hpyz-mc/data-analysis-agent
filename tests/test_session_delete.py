import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.session.store import SessionBusyError, SessionNotFoundError, SessionStore


class SessionDeleteTests(unittest.TestCase):
    def test_store_deletes_turns_before_session_in_one_transaction(self):
        session_id = uuid4()
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [{"acquired": True}, {"id": session_id}]
        store = SessionStore("postgresql://unused")
        with patch.object(store, "_connect", return_value=connection):
            store.delete_session(session_id)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("pg_try_advisory_lock", statements[0])
        self.assertIn("FOR UPDATE", statements[1])
        self.assertIn("DELETE FROM public.analysis_turns", statements[2])
        self.assertIn("DELETE FROM public.analysis_sessions", statements[3])
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
                cursor.fetchone.side_effect = [first_row, second_row]
                store = SessionStore("postgresql://unused")
                with patch.object(store, "_connect", return_value=connection):
                    with self.assertRaises(error):
                        store.delete_session(uuid4())
                self.assertFalse(any(
                    "DELETE FROM" in call.args[0] for call in cursor.execute.call_args_list
                ))
                connection.close.assert_called_once()

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
