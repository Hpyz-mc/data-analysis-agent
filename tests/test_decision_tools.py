"""Function-calling decision path: schemas, parsing, retries and strict fallback."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from openai import APIStatusError

from app.agent.state import AgentState
from app.llm.decision_tools import decision_tools, parse_tool_decision
from app.llm.deepseek import DeepSeekClient, LLMError

PLAN = {
    "question_type": "lookup",
    "requires_sql": True,
    "metric": "记录数",
    "time_range": "未指定",
    "comparison": "不适用",
    "dimensions": [],
    "analysis_steps": ["查询记录数"],
    "python_operation": None,
    "chart_type": None,
    "analysis_context": None,
}


def decision_args(action="sql", plan=None):
    return {
        "action": action,
        "reasoning": "测试",
        "plan": plan,
        "sql": "SELECT COUNT(*) AS amount FROM public.agent_sales_sample" if action == "sql" else None,
        "python": None,
        "chart": None,
        "content": "共有 9 条。" if action == "finish" else "查询目的",
    }


def tool_message(name, arguments, *, content=None):
    call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )
    return SimpleNamespace(tool_calls=[call], content=content)


def model_response(message, finish_reason="tool_calls"):
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


class DecisionToolsTests(unittest.TestCase):
    def make_client(self, responses, *, strict=False):
        client = object.__new__(DeepSeekClient)
        client.model = "test"
        client.thinking = "disabled"
        client._use_tools = True
        client._strict_ok = strict
        client._beta_client = None
        client.client = MagicMock()
        client.client.chat.completions.create.side_effect = responses
        return client

    def test_parse_decide_next_step_and_finish_report(self):
        parsed = parse_tool_decision(tool_message("decide_next_step", decision_args("sql", PLAN)))
        self.assertEqual(parsed["action"], "sql")
        self.assertEqual(parsed["plan"]["question_type"], "lookup")
        report = parse_tool_decision(tool_message("finish_report", {"content": "报告正文"}))
        self.assertEqual(report["action"], "finish")
        self.assertEqual(report["content"], "报告正文")

    def test_parse_rejects_unknown_tool_and_multiple_calls(self):
        self.assertIsNone(parse_tool_decision(tool_message("other_tool", {})))
        self.assertIsNone(parse_tool_decision(
            SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(name="x", arguments="{}")),
                                        SimpleNamespace(function=SimpleNamespace(name="y", arguments="{}"))],
                            content=None)))

    def test_decision_round_uses_tools(self):
        client = self.make_client([
            model_response(tool_message("decide_next_step", decision_args("sql", PLAN))),
        ])
        result = client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(result.action, "sql")
        kwargs = client.client.chat.completions.create.call_args.kwargs
        self.assertIn("tools", kwargs)
        self.assertEqual(kwargs["tool_choice"], "required")
        self.assertNotIn("response_format", kwargs)

    def test_report_round_forces_finish_report(self):
        client = self.make_client([
            model_response(tool_message("finish_report", {"content": "报告正文"})),
        ])
        result = client.decide(AgentState(question="多少条？"), 3, "schema", report=True)
        self.assertEqual(result.action, "finish")
        self.assertEqual(result.content, "报告正文")
        kwargs = client.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["tool_choice"],
                         {"type": "function", "function": {"name": "finish_report"}})

    def test_invalid_tool_args_retry_once_then_raise(self):
        bad = decision_args("sql", PLAN)
        del bad["reasoning"]
        client = self.make_client([
            model_response(tool_message("decide_next_step", bad)),
            model_response(SimpleNamespace(tool_calls=[], content=None)),
        ])
        with self.assertRaises(LLMError) as ctx:
            client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(ctx.exception.code, "invalid_decision")
        self.assertEqual(client.client.chat.completions.create.call_count, 2)

    def test_legacy_content_fallback_still_parses(self):
        legacy = json.dumps(decision_args("sql", PLAN), ensure_ascii=False)
        client = self.make_client([
            model_response(SimpleNamespace(tool_calls=[], content=legacy), finish_reason="stop"),
        ])
        result = client.decide(AgentState(question="多少条？"), 3, "schema")
        self.assertEqual(result.action, "sql")

    def test_strict_400_falls_back_to_non_strict(self):
        request = httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")
        error = APIStatusError("bad request", response=httpx.Response(400, request=request), body=None)
        client = self.make_client([
            error,
            model_response(tool_message("finish_report", {"content": "回退成功"})),
        ], strict=True)
        client._beta_client = client.client
        result = client.decide(AgentState(question="多少条？"), 3, "schema", report=True)
        self.assertEqual(result.content, "回退成功")
        self.assertFalse(client._strict_ok)
        second = client.client.chat.completions.create.call_args_list[1].kwargs
        self.assertNotIn("strict", second["tools"][0]["function"])

    def test_strict_schemas_are_strict_compatible(self):
        tools = decision_tools(strict=True, report=False)
        self.assertEqual(len(tools), 1)
        function = tools[0]["function"]
        self.assertTrue(function["strict"])
        schema = function["parameters"]

        def check(node, path="root"):
            if not isinstance(node, dict):
                return
            if node.get("type") == "object":
                self.assertFalse(node.get("additionalProperties", True), f"{path} 缺少 additionalProperties:false")
                self.assertEqual(sorted(node.get("properties", {})), sorted(node.get("required", [])),
                                 f"{path} 的 properties 与 required 不一致")
            for key, value in node.items():
                check(value, f"{path}.{key}") if key in ("properties",) else None
            for prop in node.get("properties", {}).values():
                check(prop, f"{path}.prop")
            for item in (node.get("items") or {}).values() if isinstance(node.get("items"), dict) else []:
                check(item, f"{path}.item")

        check(schema)
        report = decision_tools(strict=True, report=True)[0]["function"]
        self.assertEqual(sorted(report["parameters"]["required"]), ["content"])


if __name__ == "__main__":
    unittest.main()
