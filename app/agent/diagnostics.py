"""Safe execution receipts: no question text, SQL, row values or raw exceptions."""
import json
from typing import Literal

from pydantic import BaseModel, Field


class ToolReceipt(BaseModel):
    round: int
    action: Literal["sql", "python", "chart"]
    outcome: Literal["success", "error", "truncated"]
    operation: str | None = None
    row_count: int | None = None


class AnalysisDiagnostics(BaseModel):
    stage: Literal["schema", "planning", "decision", "sql", "python", "chart", "report", "complete"] = "schema"
    route: Literal["unplanned", "scoped", "lookup", "concept"] = "unplanned"
    status: Literal["running", "completed", "clarification", "incomplete", "failed"] = "running"
    planning_attempts: int = 0
    last_round: int = 0
    stopped_at: str | None = None
    stop_reason: str | None = None
    tools: list[ToolReceipt] = Field(default_factory=list)


def tool_receipts(steps):
    receipts = []
    allowed = {"summary", "percent_change", "share", "profile", "attribution", "sequential_attribution", "driver_check", "line", "bar"}
    for step in steps:
        receipt = ToolReceipt(round=step.round, action=step.action, outcome="error")
        if step.action in {"python", "chart"}:
            try:
                request = json.loads(step.tool_input)
                operation = request.get("operation", request.get("chart_type"))
                receipt.operation = operation if isinstance(operation, str) and operation in allowed else None
            except (ValueError, TypeError, AttributeError):
                pass
        try:
            value = json.loads(step.observation)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            operation = value.get("operation", value.get("chart_type"))
            if isinstance(operation, str) and operation in allowed:
                receipt.operation = operation
            if step.action == "sql" and isinstance(value.get("rows"), list):
                receipt.row_count = len(value["rows"])
                receipt.outcome = "truncated" if value.get("truncated") else "success"
            elif step.action == "python" and value.get("result") is not None:
                receipt.outcome = "error" if isinstance(value["result"], dict) and value["result"].get("reconciled") is False else "success"
            elif step.action == "chart" and value.get("chart_url"):
                receipt.outcome = "success"
        except (ValueError, TypeError):
            pass
        receipts.append(receipt)
    return receipts
