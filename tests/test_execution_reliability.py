"""Bounded planning, final-call reservation and both HTTP contracts, offline."""
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.agent.diagnostics import tool_receipts
from app.agent.loop import DataAnalysisAgent
from app.agent.state import AgentStep
from app.llm.deepseek import DeepSeekClient, LLMError
from app.llm.policy import LLMLimits
from app.llm.usage import AnalysisUsage, ModelCallUsage
from app.main import app
from app.tools.python_tool import PythonAnalysisTool
from tests.test_answer_quality import FULL_SCHEMA, records
from tests.test_analysis_context import scope
from tests.test_graph_v4 import decision, plan
from tests.test_sessions import MemoryStore
from tests.test_llm_retry import model_response, decision_json, PLAN
from app.agent.state import AgentState, AnalysisPlan


def components(mode):
    context = scope()
    first = plan().model_copy(update={"analysis_context": context})
    if mode == "missing_scope":
        decisions = [decision("sql", analysis_plan=plan(), sql="SELECT forbidden")]*2
    elif mode == "transport":
        decisions = [LLMError("模拟网络中断", code="connection_error")]
    elif mode == "budget_with_data":
        decisions = [decision("sql", analysis_plan=plan(python_operation="summary"), sql="SELECT amount FROM sales"),
                     LLMError("本轮已达到模型调用次数预算", code="model_call_budget")]
    else:
        decisions = [decision("finish", analysis_plan=first), decision("finish", content="下降主要来自线下门店。")]
    llm = SimpleNamespace(decide=MagicMock(side_effect=decisions),
        usage_snapshot=lambda: AnalysisUsage(), stream_report=MagicMock())
    def stream(state, rounds, schema, emit):
        emit("delta", {"text": "下降主要来自线下门店。"})
        return "下降主要来自线下门店。"
    llm.stream_report.side_effect = stream
    sql = SimpleNamespace(get_schema=lambda: FULL_SCHEMA, run=MagicMock(side_effect=[
        json.dumps({"rows": rows, "truncated": False}) for rows in records(context)]))
    chart = SimpleNamespace(run=lambda req, steps: json.dumps({"chart_type":req.chart_type,
        "chart_url":"/api/v1/charts/offline_test.png"}))
    return llm, sql, chart


class ExecutionReliabilityTests(unittest.TestCase):
    def test_missing_scope_stops_after_one_repair_without_running_sql(self):
        llm, sql, chart = components("missing_scope")
        result = DataAnalysisAgent(llm,sql,PythonAnalysisTool(),chart).run("为什么2026年2月销售额比1月下降？")
        self.assertFalse(result.finished)
        self.assertFalse(result.requires_clarification)
        self.assertEqual(result.diagnostics.stop_reason,"planning_limit")
        self.assertEqual(result.diagnostics.planning_attempts,2)
        self.assertEqual(llm.decide.call_count,2)
        sql.run.assert_not_called()

    def test_reserved_call_is_not_spent_on_more_decisions(self):
        client = object.__new__(DeepSeekClient)
        client.model, client.limits, client._phase = "offline", LLMLimits(max_calls=6), "decision"
        client.usage_calls = [ModelCallUsage(model="offline", attempt=1, total_tokens=1) for _ in range(5)]
        with self.assertRaises(LLMError) as caught:
            client._new_call([{"content":"test"}],1)
        self.assertEqual(caught.exception.code,"report_reserved")
        self.assertEqual(len(client.usage_calls),5)
        client._phase = "report"
        self.assertEqual(client._new_call([{"content":"test"}],1).phase,"report")
        self.assertEqual(len(client.usage_calls),6)
        with self.assertRaises(LLMError) as caught:
            client._new_call([{"content":"test"}],1)
        self.assertEqual(caught.exception.code,"model_call_budget")

    def test_final_report_cannot_restart_tool_decisions(self):
        client = object.__new__(DeepSeekClient)
        client.model, client.thinking, client.client = "offline", "disabled", MagicMock()
        client.client.chat.completions.create.return_value = model_response(decision_json("sql"))
        state = AgentState(question="查询记录数", plan=AnalysisPlan(**PLAN))
        with self.assertRaises(LLMError) as caught:
            client.decide(state, 7, "schema", report=True)
        self.assertEqual(caught.exception.code, "invalid_report")
        self.assertEqual(client.client.chat.completions.create.call_count, 1)
        self.assertEqual(client.usage_calls[0].phase, "report")

    def test_budget_failure_before_report_retains_available_evidence(self):
        for streaming in (False,True):
            with self.subTest(streaming=streaming):
                llm, sql, chart = components("budget_with_data")
                events = []
                result = DataAnalysisAgent(llm,sql,PythonAnalysisTool(),chart).run("查询数据并用Python汇总",emit=(lambda *event:events.append(event)) if streaming else None)
                self.assertFalse(result.finished)
                self.assertIn("本轮仅部分完成",result.answer)
                self.assertEqual(result.diagnostics.stop_reason,"model_call_budget")
                self.assertEqual(result.diagnostics.stopped_at,"decision")
                self.assertEqual(result.diagnostics.tools[0].row_count,2)
                sql.run.assert_called_once()
                llm.stream_report.assert_not_called()

    def test_diagnostics_exclude_raw_queries_values_and_tool_errors(self):
        steps = [AgentStep(round=1,action="sql",reasoning="私人问题",tool_input="SELECT secret FROM private_table",observation=json.dumps({"rows":[{"secret":"私人数据"}],"truncated":False})),
                 AgentStep(round=2,action="python",reasoning="secret",tool_input="private_table",observation="工具错误：私人数据与数据库连接串")]
        encoded = json.dumps([r.model_dump() for r in tool_receipts(steps)],ensure_ascii=False)
        for text in ("SELECT", "secret", "private_table", "私人", "连接串"):
            self.assertNotIn(text,encoded)

    def test_http_and_sse_complete_incomplete_and_failed_history(self):
        for mode in ("success","missing_scope","transport","budget_with_data"):
            for streaming in (False,True):
                with self.subTest(mode=mode,streaming=streaming):
                    llm, sql, chart = components(mode)
                    store = MemoryStore()
                    with ExitStack() as stack:
                        stack.enter_context(patch("app.api.analysis.get_settings",return_value=SimpleNamespace(session_database_url="offline")))
                        stack.enter_context(patch("app.api.analysis.SessionStore",return_value=store))
                        stack.enter_context(patch("app.api.analysis.DeepSeekClient",return_value=llm))
                        stack.enter_context(patch("app.api.analysis.SQLTool",return_value=sql))
                        stack.enter_context(patch("app.api.analysis.ChartTool",return_value=chart))
                        question = "查询数据并用Python汇总" if mode == "budget_with_data" else "为什么2026年2月销售额比1月下降？"
                        response = TestClient(app).post("/api/v1/analyze"+("/stream" if streaming else ""),json={"question":question})
                    if streaming:
                        events = [(block.splitlines()[0][7:],json.loads(block.splitlines()[1][6:])) for block in response.text.strip().split("\n\n") if block.startswith("event:")]
                        event, payload = events[-1]
                        self.assertEqual(event,"error" if mode=="transport" else "result")
                        if mode=="success":
                            self.assertEqual(sum(e=="delta" for e,_ in events),1)
                    else:
                        self.assertEqual(response.status_code,502 if mode=="transport" else 200)
                        payload = response.json()
                    expected = {"success":"completed","missing_scope":"incomplete","transport":"failed","budget_with_data":"incomplete"}[mode]
                    record = next(iter(store.turns.values()))[0]
                    self.assertEqual(record["status"],expected)
                    self.assertEqual(payload["diagnostics"]["status"],expected)
                    self.assertEqual(record["artifacts"]["diagnostics"],payload["diagnostics"])
                    if mode=="success":
                        self.assertTrue(payload["finished"])
                        self.assertEqual(sql.run.call_count,3)
                        self.assertEqual(llm.decide.call_count+llm.stream_report.call_count,2)
                    elif mode == "budget_with_data":
                        self.assertFalse(payload["finished"])
                        self.assertIn("本轮仅部分完成", payload["answer"])
                        self.assertEqual(payload["diagnostics"]["stop_reason"],"model_call_budget")
                        self.assertEqual(payload["diagnostics"]["stopped_at"],"decision")
                        sql.run.assert_called_once()
                    else:
                        self.assertEqual(payload["diagnostics"]["stopped_at"],"planning")
                        sql.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
