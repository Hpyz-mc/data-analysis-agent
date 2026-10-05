import unittest

from app.presentation import display_time, history_messages, response_charts


class UIPresentationTests(unittest.TestCase):
    def test_history_rebuilds_chat_and_chart(self):
        messages = history_messages([{
            "question": "画图",
            "answer": "图表：/api/v1/charts/example.png",
            "status": "completed",
        }])
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["charts"], ["/api/v1/charts/example.png"])

    def test_response_accepts_charts_and_chart_url(self):
        self.assertEqual(
            response_charts({
                "charts": ["/api/v1/charts/a.png"],
                "chart_url": "/api/v1/charts/a.png",
            }),
            ["/api/v1/charts/a.png"],
        )

    def test_created_time_is_shanghai_local(self):
        self.assertEqual(display_time("2026-09-28T08:00:00Z"), "09-28 16:00")


if __name__ == "__main__":
    unittest.main()
