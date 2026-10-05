import io
import json
import unittest
from unittest.mock import patch

from app.clients import local_api as api_client


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class UIClientTests(unittest.TestCase):
    @patch("app.clients.local_api.urlopen")
    def test_upload_dataset_uses_multipart(self, open_mock):
        open_mock.return_value = FakeResponse(b'{"status":"success"}')
        result = api_client.upload_dataset(b'a,b\n1,2', 'sales.csv', None)
        self.assertEqual(result["status"], "success")
        request = open_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("multipart/form-data", request.headers["Content-type"])
        self.assertIn(b'a,b\n1,2', request.data)

    @patch("app.clients.local_api.urlopen")
    def test_preset_uses_post(self, open_mock):
        open_mock.return_value = FakeResponse(b'{"status":"success"}')
        api_client.load_preset_dataset()
        request = open_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/api/v1/datasets/preset?preset_type=ecommerce"))

    @patch("app.clients.local_api.urlopen")
    def test_delete_session_uses_delete_method(self, open_mock):
        response = FakeResponse(b"")
        response.status = 204
        open_mock.return_value = response
        self.assertIsNone(api_client.delete_session("test-id"))
        request = open_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertTrue(request.full_url.endswith("/api/v1/sessions/test-id"))

    @patch("app.clients.local_api.urlopen")
    def test_list_sessions_uses_collection_endpoint(self, open_mock):
        open_mock.return_value = FakeResponse(b'{"sessions":[]}')
        self.assertEqual(api_client.list_sessions(), {"sessions": []})
        request = open_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/api/v1/sessions"))

    @patch("app.clients.local_api.urlopen")
    def test_analyze_omits_session_id_for_new_conversation(self, open_mock):
        open_mock.return_value = FakeResponse(b'{"answer":"ok"}')
        result = api_client.analyze("测试问题", None)
        self.assertEqual(result["answer"], "ok")
        request = open_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"question": "测试问题"})

    @patch("app.clients.local_api.urlopen")
    def test_analyze_keeps_session_id_for_follow_up(self, open_mock):
        open_mock.return_value = FakeResponse(b'{"answer":"ok"}')
        api_client.analyze("追问", "abc")
        request = open_mock.call_args.args[0]
        self.assertEqual(json.loads(request.data)["session_id"], "abc")

    @patch("app.clients.local_api.urlopen")
    def test_chart_rejects_external_and_parent_paths(self, open_mock):
        for path in ("https://example.com/a.png", "/api/v1/charts/../x", "/other.png"):
            with self.subTest(path=path), self.assertRaises(api_client.APIError):
                api_client.get_chart(path)
        open_mock.assert_not_called()

    @patch("app.clients.local_api.urlopen")
    def test_chart_fetches_local_path(self, open_mock):
        open_mock.return_value = FakeResponse(b"png bytes")
        result = api_client.get_chart("/api/v1/charts/test.png")
        self.assertEqual(result, b"png bytes")
        self.assertEqual(
            open_mock.call_args.args[0],
            api_client.BASE_URL + "/api/v1/charts/test.png",
        )


if __name__ == "__main__":
    unittest.main()
