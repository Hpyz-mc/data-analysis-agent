import unittest
from types import SimpleNamespace

from app.agent.state import AgentState
from app.llm.usage import AnalysisUsage, ModelCallUsage
from tests import test_llm_retry as retry_helpers


class TokenUsageTests(unittest.TestCase):
    def test_retry_usage_includes_invalid_output(self):
        responses = [retry_helpers.model_response(None),
                     retry_helpers.model_response(retry_helpers.decision_json("finish"))]
        for response in responses:
            response.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                             total_tokens=120, prompt_cache_hit_tokens=50)
        client = retry_helpers.LLMRetryTests().make_client(responses)
        client.decide(AgentState(question="测试"), 3, "schema")
        usage = client.usage_snapshot()
        self.assertEqual((usage.total_tokens, usage.model_calls, usage.retries), (240, 2, 1))
        self.assertEqual(usage.cache_hit_tokens, 100)
        self.assertTrue(usage.complete)

    def test_unknown_usage_is_not_zero(self):
        usage = AnalysisUsage.aggregate([ModelCallUsage(model="test", attempt=1)])
        self.assertIsNone(usage.total_tokens)
        self.assertFalse(usage.complete)
        self.assertEqual(usage.unknown_calls, 1)
