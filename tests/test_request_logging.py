"""V3 request observability checks without calling the database or LLM."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class RequestLoggingTests(unittest.TestCase):
    def test_health_response_has_request_id_and_safe_log(self):
        with patch("app.main.logger") as logger:
            response = TestClient(app).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(len(response.headers["X-Request-ID"]), 32)
        logged = logger.info.call_args.args
        self.assertIn("request_complete", logged[0])
        self.assertIn("/health", logged)
        self.assertNotIn("LLM_API_KEY", str(logged))

    def test_validation_error_has_request_id(self):
        with patch("app.main.logger") as logger:
            response = TestClient(app).post("/api/v1/analyze", json={"question": "a"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(response.headers["X-Request-ID"]), 32)
        self.assertIn(422, logger.info.call_args.args)


if __name__ == "__main__":
    unittest.main()
