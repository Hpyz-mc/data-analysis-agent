"""V4 graph routing checks without DeepSeek or a live database."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agent.loop import DataAnalysisAgent
from app.agent.analysis_policy import build_analysis_task
from app.agent.semantics import semantic_sql_error
from app.agent.state import AnalysisPlan, ConversationTurn
from app.agent.context import AnalysisContext
from app.tools.chart_tool import ChartRequest
from app.tools.python_tool import PythonAnalysisRequest


def plan(*, requires_sql=True, python_operation=None, chart_type=None):
    return AnalysisPlan(
        question_type="lookup" if requires_sql else "concept",
        requires_sql=requires_sql,
        metric="销售额" if requires_sql else "不适用",
        time_range="2026 年第一季度" if requires_sql else "未指定",
        comparison="不适用",
        analysis_steps=["查询并回答"] if requires_sql else ["解释概念"],
        python_operation=python_operation,
        chart_type=chart_type,
    )


def decision(action, *, analysis_plan=None, sql=None, python=None, chart=None, content=""):
    return SimpleNamespace(
        action=action, plan=analysis_plan, sql=sql, python=python, chart=chart,
        content=content, reasoning=f"执行 {action}",
    )


def make_agent(decisions, *, max_rounds=7):
    llm = SimpleNamespace(decide=MagicMock(side_effect=decisions))
    sql = SimpleNamespace(
        get_schema=MagicMock(return_value="public.sales: amount (numeric)"),
        run=MagicMock(return_value=json.dumps({
            "rows": [{"category": "电脑", "amount": "100"}],
            "row_count": 1, "truncated": False,
        })),
    )
    python = SimpleNamespace(run=MagicMock(return_value=json.dumps({
        "operation": "summary", "result": {"sum": "100"},
    })))
    chart = SimpleNamespace(run=MagicMock(return_value=json.dumps({
        "chart_type": "bar", "chart_url": "/charts/test.png",
    })))
    return DataAnalysisAgent(llm, sql, python, chart, max_rounds=max_rounds), llm, sql, python, chart


class GraphV4Tests(unittest.TestCase):
    def test_analysis_task_uses_the_right_comparison_axis(self):
        self.assertEqual(build_analysis_task("为什么2月比1月下降？").kind, "period_change")
        self.assertEqual(build_analysis_task("为什么A地区比B地区高？").kind, "segment_comparison")
        self.assertEqual(build_analysis_task("分析各月销售额走势").kind, "trend")
        self.assertEqual(build_analysis_task("为什么2月销售额下降？").kind, "clarification")
        self.assertEqual(build_analysis_task("2月销售额下降多少？").kind, "lookup")

    def test_known_non_additive_field_is_not_summed_as_calendar_days(self):
        self.assertIsNotNone(semantic_sql_error(
            "SELECT SUM(stockout_days) FROM public.demo_ecommerce_orders"
        ))
        self.assertIsNone(semantic_sql_error(
            "SELECT MAX(stockout_days) FROM public.demo_ecommerce_orders"
        ))

    def test_clarification_turn_restores_original_comparison_question(self):
        agent, llm, sql, *_ = make_agent([])
        clarification = agent.run("为什么2月销售额下降？")
        self.assertFalse(clarification.finished)
        self.assertIn("比较基准", clarification.answer)
        llm.decide.assert_not_called()
        sql.run.assert_not_called()

        followup = ConversationTurn(
            question="为什么2月销售额下降？", answer=clarification.answer,
        )
        llm.decide.side_effect = RuntimeError("stop after inspecting the task")
        with self.assertRaisesRegex(RuntimeError, "stop after inspecting"):
            agent.run("与1月相比", [followup])
        self.assertEqual(llm.decide.call_count, 1)
        self.assertEqual(llm.decide.call_args.args[0].task.kind, "period_change")

    def test_ambiguous_versions_ask_before_llm_or_sql(self):
        agent, llm, sql, *_ = make_agent([])
        sql.find_ambiguous_imports = MagicMock(return_value="请指定版本 A 或 B")
        response = agent.run("分析销售额")
        self.assertFalse(response.finished)
        self.assertEqual(response.answer, "请指定版本 A 或 B")
        llm.decide.assert_not_called()
        sql.run.assert_not_called()

    def test_graph_has_explicit_decision_tool_and_report_nodes(self):
        agent, *_ = make_agent([])
        nodes = set(agent._build_graph().get_graph().nodes)
        self.assertTrue({"load_schema", "decide", "sql", "python", "chart", "report"} <= nodes)

    def test_concept_question_finishes_without_tools(self):
        agent, llm, sql, python, chart = make_agent([
            decision("finish", analysis_plan=plan(requires_sql=False), content="这是概念解释。"),
        ])
        response = agent.run("什么是销售额？")
        self.assertTrue(response.finished)
        self.assertEqual(response.rounds, 1)
        self.assertEqual(response.answer, "这是概念解释。")
        self.assertEqual(response.steps, [])
        self.assertIn("total", response.timings_ms)
        llm.decide.assert_called_once()
        sql.run.assert_not_called()
        python.run.assert_not_called()
        chart.run.assert_not_called()

    def test_sql_error_then_corrected_query_reaches_report(self):
        first_sql = "SELECT bad FROM public.sales"
        second_sql = "SELECT SUM(amount) AS total FROM public.sales"
        agent, llm, sql, *_ = make_agent([
            decision("sql", analysis_plan=plan(), sql=first_sql),
            decision("sql", sql=second_sql),
            decision("finish", content="销售额为 100。"),
        ])
        from app.tools.sql_tool import SQLToolError
        sql.run.side_effect = [SQLToolError("wrong column"), json.dumps({
            "rows": [{"total": "100"}], "row_count": 1, "truncated": False,
        })]
        response = agent.run("查询销售额")
        self.assertTrue(response.finished)
        self.assertEqual(response.rounds, 3)
        self.assertEqual([step.action for step in response.steps], ["sql", "sql"])
        self.assertEqual(sql.run.call_count, 2)
        self.assertIn("工具错误", response.steps[0].observation)
        self.assertEqual(llm.decide.call_count, 3)

    def test_python_and_chart_nodes_run_before_final_answer(self):
        python_request = PythonAnalysisRequest(
            operation="summary", source_round=1, value_column="amount",
        )
        chart_request = ChartRequest(
            chart_type="bar", source_round=1, x_column="category",
            y_column="amount", title="类别销售额",
        )
        agent, llm, sql, python, chart = make_agent([
            decision("sql", analysis_plan=plan(python_operation="summary", chart_type="bar"),
                     sql="SELECT category, amount FROM public.sales"),
            decision("python", python=python_request),
            decision("chart", chart=chart_request),
            decision("finish", content="已分析并生成图表。"),
        ])
        response = agent.run("分析并画图")
        self.assertTrue(response.finished)
        self.assertEqual([step.action for step in response.steps], ["sql", "python", "chart"])
        self.assertEqual(response.charts, ["/charts/test.png"])
        self.assertEqual(response.rounds, 4)
        self.assertIn("python_round_2", response.timings_ms)
        self.assertIn("chart_round_3", response.timings_ms)
        sql.run.assert_called_once()
        python.run.assert_called_once()
        chart.run.assert_called_once()
        self.assertEqual(llm.decide.call_count, 4)

    def test_missing_first_plan_consumes_round_without_running_tool(self):
        agent, llm, sql, *_ = make_agent([
            decision("sql", sql="SELECT 1"),
            decision("finish", analysis_plan=plan(requires_sql=False), content="概念回答"),
        ])
        response = agent.run("解释概念")
        self.assertTrue(response.finished)
        self.assertEqual(response.rounds, 2)
        sql.run.assert_not_called()
        self.assertEqual(llm.decide.call_count, 2)

    def test_last_round_tool_call_remains_incomplete(self):
        agent, llm, sql, *_ = make_agent([
            decision("sql", analysis_plan=plan(), sql="SELECT amount FROM public.sales"),
        ], max_rounds=1)
        response = agent.run("查询销售额")
        self.assertFalse(response.finished)
        self.assertEqual(response.rounds, 1)
        self.assertEqual(len(response.steps), 1)
        self.assertIn("未能生成最终报告", response.answer)
        llm.decide.assert_called_once()
        sql.run.assert_called_once()

    def test_diagnosis_rejects_early_finish_until_attribution_is_verified(self):
        context = AnalysisContext(kind="period_change", source_table="public.sales", metric_label="销售额",
            metric_expression="amount", time_column="order_date", dimension="channel",
            baseline={"start":"2026-01-01", "end":"2026-02-01"},
            current={"start":"2026-02-01", "end":"2026-03-01"})
        initial = plan().model_copy(update={"analysis_context": context})
        base, current = context.baseline.label(), context.current.label()
        agent, llm, sql, python, _ = make_agent([
            decision("finish", analysis_plan=initial, content="2月下降了。"),
            decision("finish", content="### 核心发现\n线下门店贡献了主要降幅；原因待核实。"),
        ])
        sql.get_schema.return_value = "public.sales: amount (numeric), order_date (date), channel (text)"
        sql.run.side_effect = [
            json.dumps({"rows": [{"period": base, "amount": "100"},
                                 {"period": current, "amount": "80"}], "truncated": False}),
            json.dumps({"rows": [{"period": base, "dimension": "线下门店", "amount": "100"},
                                 {"period": current, "dimension": "线下门店", "amount": "80"}],
                        "truncated": False}),
        ]
        python.run.return_value = json.dumps({
            "operation": "attribution", "result": {
                "baseline_period": base, "current_period": current,
                "baseline_total": "100", "current_total": "80", "total_change": "-20",
                "details": [{"dimension": "线下门店", "change": "-20"}],
                "reconciled": True,
            },
        })

        response = agent.run("为什么2月销售额比1月下降？")

        self.assertTrue(response.finished)
        self.assertEqual(response.rounds, 5)
        self.assertEqual([step.action for step in response.steps], ["sql", "sql", "python", "chart"])
        self.assertEqual(response.plan.python_operation, "attribution")
        self.assertIn("变化率：-20.00%", response.answer)
        self.assertIn("变化贡献不等于因果原因", response.answer)
        self.assertEqual(llm.decide.call_count, 2)

    def test_trend_requires_calculated_change(self):
        context = AnalysisContext(kind="trend", source_table="public.sales", metric_label="销售额",
            metric_expression="amount", time_column="order_date", time_grain="month",
            current={"start":"2026-01-01", "end":"2026-03-01"})
        agent, _, sql, python, chart = make_agent([
            decision("finish", analysis_plan=plan().model_copy(update={"analysis_context": context})),
            decision("finish", content="2月比1月下降20%。"),
        ])
        sql.get_schema.return_value = "public.sales: amount (numeric), order_date (date)"
        chart.run.return_value = json.dumps({"chart_type": "line", "chart_url": "/charts/test.png"})
        sql.run.return_value = json.dumps({
            "rows": [{"period": "2026-01", "amount": "100"},
                     {"period": "2026-02", "amount": "80"}], "truncated": False,
        })
        python.run.return_value = json.dumps({
            "operation": "percent_change", "result": [
                {"label": "1月", "value": "100", "difference": None},
                {"label": "2月", "value": "80", "difference": "-20"},
            ],
        })

        response = agent.run("分析各月销售额走势")

        self.assertTrue(response.finished)
        self.assertEqual(response.rounds, 4)
        self.assertEqual(response.plan.python_operation, "percent_change")

    def test_region_comparison_uses_groups_not_time_periods(self):
        context = AnalysisContext(kind="segment_comparison", source_table="public.sales", metric_label="销售额",
            metric_expression="amount", group_column="region", baseline_group="A地区", current_group="B地区", dimension="category")
        agent, _, sql, python, _ = make_agent([
            decision("finish", analysis_plan=plan().model_copy(update={"analysis_context": context})),
            decision("finish", content="B地区高于A地区；贡献来自电脑品类。"),
        ])
        sql.get_schema.return_value = "public.sales: amount (numeric), region (text), category (text)"
        sql.run.side_effect = [
            json.dumps({"rows": [{"period": "A地区", "amount": "80"},
                                 {"period": "B地区", "amount": "100"}], "truncated": False}),
            json.dumps({"rows": [{"period": "A地区", "dimension": "电脑", "amount": "80"},
                                 {"period": "B地区", "dimension": "电脑", "amount": "100"}],
                        "truncated": False}),
        ]
        python.run.return_value = json.dumps({"operation": "attribution", "result": {
            "baseline_period": "A地区", "current_period": "B地区", "baseline_total": "80",
            "current_total": "100", "total_change": "20", "reconciled": True,
            "details": [{"dimension": "电脑", "change": "20"}],
        }})

        response = agent.run("为什么A地区比B地区销售额低？")

        self.assertTrue(response.finished)
        self.assertEqual(response.plan.question_type, "diagnosis")
        self.assertIn("A地区：80", response.answer)
        self.assertIn("B地区：100", response.answer)


if __name__ == "__main__":
    unittest.main()
