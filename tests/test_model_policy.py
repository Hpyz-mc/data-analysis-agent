"""Focused regressions for grain, evidence selection and bounded transport."""
import json
import unittest
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from openai import APIConnectionError

from app.agent.intent import grain_feedback, query_matches_grain
from app.agent.prompt_context import tool_reference
from app.agent.scoped_analysis import align_time_intent, build_pipeline, validate_context
from app.llm.deepseek import DeepSeekClient, LLMError
from app.llm.policy import LLMLimits, budget_error
from app.llm.usage import ModelCallUsage
from tests.test_analysis_context import SCHEMA, scope


class ModelPolicyTests(unittest.TestCase):
    def test_monthly_scope_cannot_become_total(self):
        context = scope(kind="lookup", dimension=None, baseline=None)
        context = align_time_intent(validate_context(context, SCHEMA), "每月销售额", SCHEMA)
        sql = build_pipeline(context, 1)[0]["sql"]
        self.assertTrue(query_matches_grain(sql, "month"))
        step = SimpleNamespace(action="sql", tool_input="SELECT SUM(sales_amount) FROM sales",
                               observation=json.dumps({"rows": [{"amount": 10}], "truncated": False}))
        self.assertIn("不能用范围合计代替", grain_feedback("每月销售额", [step]))
        step.tool_input = sql
        self.assertEqual(grain_feedback("每月销售额", [step]), "")
        total = align_time_intent(context, "那合计多少？", SCHEMA)
        self.assertIsNone(total.time_grain)

    def test_supported_time_buckets_and_aliases(self):
        for grain in ("day", "week", "month", "quarter", "year"):
            sql = f"SELECT date_trunc('{grain}', order_date) AS period, SUM(amount) FROM sales GROUP BY period"
            self.assertTrue(query_matches_grain(sql, grain))
        self.assertFalse(query_matches_grain("SELECT SUM(amount) FROM sales", "month"))

    def test_preview_does_not_replace_report_evidence(self):
        raw = json.dumps({"rows": [{"amount": n} for n in range(10)], "truncated": False})
        step = SimpleNamespace(round=1, action="sql", tool_input="SELECT amount FROM sales", observation=raw)
        self.assertIn('"omitted_from_prompt":7', tool_reference([step]))
        self.assertIn('"amount": 9', tool_reference([step], report=True))
        self.assertEqual(step.observation, raw)

    def test_unknown_usage_reserves_budget_and_call_limit(self):
        call = ModelCallUsage(model="test", attempt=1, estimated_prompt_tokens=900, output_token_limit=200)
        messages = [{"content": "x"}]
        self.assertIn("Token", budget_error(LLMLimits(request_token_budget=1200), [call], messages, monotonic()))
        self.assertIn("调用次数", budget_error(LLMLimits(max_calls=1), [call], messages, monotonic()))

    def test_initial_transport_retry_is_counted(self):
        client = object.__new__(DeepSeekClient)
        client.model, client.thinking = "test", "disabled"
        client.limits = LLMLimits(network_retries=1)
        client.client = MagicMock()
        client.client.chat.completions.create.side_effect = [
            APIConnectionError(request=httpx.Request("POST", "https://example.invalid")),
            SimpleNamespace(choices=[]),
        ]
        with patch("app.llm.deepseek.sleep"):
            client._completion([{"role": "user", "content": "test"}], 1)
        self.assertEqual(len(client.usage_calls), 2)
        self.assertEqual(client.usage_calls[1].retry_reason, "network")
        self.assertIsNone(client.usage_calls[0].total_tokens)
        self.assertEqual(client.client.chat.completions.create.call_args.kwargs["max_tokens"], 1800)
        client.limits = LLMLimits(max_calls=2)
        with self.assertRaises(LLMError):
            client._completion([{"role": "user", "content": "test"}], 1)
        self.assertEqual(client.client.chat.completions.create.call_count, 2)
