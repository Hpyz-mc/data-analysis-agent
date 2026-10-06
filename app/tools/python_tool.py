"""Bounded Python calculations over rows returned by the SQL tool."""

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.agent.state import AgentStep
from app.tools.driver_check import driver_check


class PythonToolError(ValueError):
    """The requested analysis cannot be performed on the selected SQL result."""


class PythonAnalysisRequest(BaseModel):
    operation: Literal["summary", "percent_change", "share", "profile", "attribution", "sequential_attribution", "driver_check", "decile"]
    source_round: int = Field(ge=1)
    value_column: str | None = None
    label_column: str | None = None
    total_source_round: int | None = Field(default=None, ge=1)
    period_column: str | None = None
    dimension_column: str | None = None
    baseline_period: str | None = None
    current_period: str | None = None
    order_column: str | None = None
    comparison_periods: list[str] | None = Field(default=None, min_length=2, max_length=30)

    @model_validator(mode="after")
    def require_label_when_needed(self) -> "PythonAnalysisRequest":
        if self.operation != "profile" and not self.value_column:
            raise ValueError("数值计算需要 value_column。")
        if self.operation in {"percent_change", "share"} and not self.label_column:
            raise ValueError("percent_change 和 share 需要 label_column。")
        if self.operation in {"attribution", "sequential_attribution", "driver_check"}:
            if not all((self.total_source_round, self.period_column, self.dimension_column)):
                raise ValueError("归因需要独立总额轮次、时期列和维度列。")
            if self.total_source_round == self.source_round:
                raise ValueError("总额查询和维度明细必须来自不同 SQL 轮次。")
        if self.operation == "decile":
            if not self.total_source_round:
                raise ValueError("十分位分层需要独立总额轮次。")
            if self.total_source_round == self.source_round:
                raise ValueError("分层查询和独立总额必须来自不同 SQL 轮次。")
        if self.operation == "attribution":
            if not all(
                (
                    self.total_source_round,
                    self.period_column,
                    self.dimension_column,
                    self.baseline_period,
                    self.current_period,
                )
            ):
                raise ValueError("attribution 需要总额轮次、时期列、维度列和两个时期。")
            if self.total_source_round == self.source_round:
                raise ValueError("总额查询和维度明细必须来自不同 SQL 轮次。")
            if self.baseline_period == self.current_period:
                raise ValueError("对比的两个时期必须不同。")
        return self


class PythonAnalysisTool:
    """Calculate statistics without executing model-generated Python source."""

    def run(self, request: PythonAnalysisRequest, steps: list[AgentStep]) -> str:
        rows = self._rows_from_sql(request.source_round, steps)
        if request.operation == "driver_check":
            if (request.period_column, request.dimension_column, request.value_column) != ("period", "dimension", "amount") or request.order_column not in (None, "orders"):
                raise PythonToolError("辅助核验只接受程序生成的 period/dimension/amount/orders 列。")
            try:
                result = driver_check(rows, self._rows_from_sql(request.total_source_round, steps),
                                      with_orders=request.order_column is not None,
                                      period_order=request.comparison_periods)
            except ValueError as exc:
                raise PythonToolError(str(exc)) from exc
            return json.dumps({"operation": "driver_check", "source_round": request.source_round,
                               "result": result}, ensure_ascii=False)
        if request.operation == "sequential_attribution":
            return self._sequential_attribution(request, rows, steps)
        if request.operation == "attribution":
            return self._attribution(request, rows, steps)
        if request.operation == "decile":
            if request.value_column != "amount":
                raise PythonToolError("十分位分层只接受程序生成的 amount 列。")
            return self._decile(request, rows, steps)

        if request.operation == "profile":
            if not all(isinstance(row, dict) for row in rows):
                raise PythonToolError("查询结果不是表格行，无法检查数据质量。")
            frame = pd.DataFrame(rows)
            canonical_rows = pd.Series(
                [json.dumps(row, sort_keys=True, default=str) for row in rows]
            )
            result = {
                "row_count": len(frame),
                "columns": list(frame.columns),
                "missing_by_column": {
                    key: int(value) for key, value in frame.isna().sum().items()
                },
                "empty_string_by_column": {
                    key: int(value)
                    for key, value in frame.apply(
                        lambda column: column.map(
                            lambda value: isinstance(value, str) and not value.strip()
                        ).sum()
                    ).items()
                },
                "duplicate_rows": int(canonical_rows.duplicated().sum()),
            }
            return json.dumps(
                {"operation": "profile", "source_round": request.source_round, "result": result},
                ensure_ascii=False,
            )

        values: list[Decimal] = []
        labels: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or request.value_column not in row:
                raise PythonToolError(f"查询结果没有数值列 {request.value_column}。")
            try:
                value = Decimal(str(row[request.value_column]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise PythonToolError(
                    f"列 {request.value_column} 包含非数值或空值。"
                ) from exc
            if not value.is_finite():
                raise PythonToolError("数值列不能包含无穷大或 NaN。")
            values.append(value)

            if request.label_column:
                if request.label_column not in row:
                    raise PythonToolError(f"查询结果没有标签列 {request.label_column}。")
                labels.append(str(row[request.label_column]))

        if request.operation == "summary":
            total = sum(values, Decimal(0))
            result = {
                "count": len(values),
                "sum": str(total),
                "mean": str(total / len(values)),
                "median": str(median(values)),
                "min": str(min(values)),
                "max": str(max(values)),
            }
        elif request.operation == "percent_change":
            result = []
            for index, (label, value) in enumerate(zip(labels, values)):
                previous = values[index - 1] if index else None
                result.append(
                    {
                        "label": label,
                        "value": str(value),
                        "difference": str(value - previous) if previous is not None else None,
                        "percent_change": (
                            str((value - previous) / previous * 100)
                            if previous not in (None, 0)
                            else None
                        ),
                    }
                )
        else:
            total = sum(values, Decimal(0))
            if total == 0:
                raise PythonToolError("总和为 0，无法计算占比。")
            result = {
                "total": str(total),
                "shares": [
                    {
                        "label": label,
                        "value": str(value),
                        "percentage": str(value / total * 100),
                    }
                    for label, value in zip(labels, values)
                ],
            }

        return json.dumps(
            {
                "operation": request.operation,
                "source_round": request.source_round,
                "result": result,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _rows_from_sql(source_round: int, steps: list[AgentStep]) -> list[dict]:
        source = next(
            (
                step
                for step in steps
                if step.round == source_round and step.action == "sql"
            ),
            None,
        )
        if source is None:
            raise PythonToolError("source_round 必须指向此前成功的 SQL 轮次。")

        try:
            observation = json.loads(source.observation)
        except json.JSONDecodeError as exc:
            raise PythonToolError("该 SQL 轮次没有可分析的查询结果。") from exc

        rows = observation.get("rows") if isinstance(observation, dict) else None
        if not isinstance(rows, list) or not rows:
            raise PythonToolError("查询结果为空，无法计算。")
        if observation.get("truncated"):
            raise PythonToolError("查询结果已截断，请先用 SQL 缩小范围或汇总后再分析。")
        if not all(isinstance(row, dict) for row in rows):
            raise PythonToolError("SQL 结果必须是表格行。")
        return rows

    def _attribution(
        self,
        request: PythonAnalysisRequest,
        detail_rows: list[dict],
        steps: list[AgentStep],
        *, total_rows: list[dict] | None = None,
    ) -> str:
        total_round = request.total_source_round
        period_column = request.period_column
        dimension_column = request.dimension_column
        value_column = request.value_column
        baseline = request.baseline_period
        current = request.current_period
        if not all(
            (total_round, period_column, dimension_column, value_column, baseline, current)
        ):
            raise PythonToolError("归因参数不完整。")

        if total_rows is None:
            total_rows = self._rows_from_sql(total_round, steps)
        detail = pd.DataFrame(detail_rows)
        totals = pd.DataFrame(total_rows)
        if not {period_column, dimension_column, value_column}.issubset(detail.columns):
            raise PythonToolError("维度明细缺少时期、维度或数值列。")
        if not {period_column, value_column}.issubset(totals.columns):
            raise PythonToolError("总额查询缺少时期或数值列。")

        for frame in (detail, totals):
            if frame[period_column].isna().any() or frame[value_column].isna().any():
                raise PythonToolError("时期或数值列有空值，不能进行归因。")
            frame["_period"] = frame[period_column].map(str)
            frame["_amount"] = frame[value_column].map(self._decimal_value)
            if frame["_period"].str.strip().eq("").any():
                raise PythonToolError("时期列包含空字符串。")
            if not set(frame["_period"]).issubset({baseline, current}):
                raise PythonToolError("SQL 结果包含对比时期以外的数据，请先限制时间范围。")

        if detail[dimension_column].isna().any():
            raise PythonToolError("维度列有空值，不能进行完整归因。")
        detail["_dimension"] = detail[dimension_column].map(str)
        if detail["_dimension"].str.strip().eq("").any():
            raise PythonToolError("维度列包含空字符串。")
        if len(totals) != 2 or set(totals["_period"]) != {baseline, current}:
            raise PythonToolError("总额查询必须恰好返回两个时期、每期一行。")

        total_by_period = dict(zip(totals["_period"], totals["_amount"]))
        grouped = detail.groupby(["_period", "_dimension"], sort=True)["_amount"].sum()
        detail_by_period = {
            period: sum(
                (
                    amount
                    for (group_period, _), amount in grouped.items()
                    if group_period == period
                ),
                Decimal(0),
            )
            for period in (baseline, current)
        }
        for period in (baseline, current):
            if detail_by_period[period] != total_by_period[period]:
                raise PythonToolError(
                    f"{period} 的维度明细合计 {detail_by_period[period]} "
                    f"与独立总额 {total_by_period[period]} 不一致，不能归因。"
                )

        total_change = total_by_period[current] - total_by_period[baseline]
        dimensions = sorted({dimension for _, dimension in grouped.index})
        zero_filled: list[dict[str, str]] = []
        changes: list[dict[str, str | None]] = []
        for dimension in dimensions:
            base_key = (baseline, dimension)
            current_key = (current, dimension)
            if base_key not in grouped.index:
                zero_filled.append({"period": baseline, "dimension": dimension})
            if current_key not in grouped.index:
                zero_filled.append({"period": current, "dimension": dimension})
            base_value = grouped.get(base_key, Decimal(0))
            current_value = grouped.get(current_key, Decimal(0))
            change = current_value - base_value
            contribution = (
                str(
                    (change / total_change * 100).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
                if total_change != 0
                else None
            )
            changes.append(
                {
                    "dimension": dimension,
                    "baseline_value": str(base_value),
                    "current_value": str(current_value),
                    "change": str(change),
                    "contribution_percent": contribution,
                }
            )
        changes.sort(key=lambda item: abs(Decimal(item["change"])), reverse=True)
        reconciled = (
            sum((Decimal(item["change"]) for item in changes), Decimal(0))
            == total_change
        )
        if not reconciled:
            raise PythonToolError("各维度变化额无法加回总变化额，不能得出归因结论。")

        return json.dumps(
            {
                "operation": "attribution",
                "source_round": request.source_round,
                "total_source_round": total_round,
                "result": {
                    "baseline_period": baseline,
                    "current_period": current,
                    "baseline_total": str(total_by_period[baseline]),
                    "current_total": str(total_by_period[current]),
                    "total_change": str(total_change),
                    "details": changes,
                    "reconciled": True,
                    "zero_filled_missing_pairs": zero_filled,
                    "caution": "变化贡献不等于因果原因。",
                },
            },
            ensure_ascii=False,
        )

    def _decile(self, request: PythonAnalysisRequest, rows: list[dict],
                steps: list[AgentStep]) -> str:
        """Reconcile decile buckets against an independent total, to the cent."""
        total_rows = self._rows_from_sql(request.total_source_round, steps)
        if len(total_rows) != 1 or "amount" not in total_rows[0]:
            raise PythonToolError("独立总额必须恰好一行。")
        total = self._decimal_value(total_rows[0]["amount"])
        buckets = []
        for row in rows:
            if "bucket" not in row or "amount" not in row:
                raise PythonToolError("分层查询缺少 bucket 或 amount 列。")
            buckets.append({
                "bucket": str(row["bucket"]),
                "amount": self._decimal_value(row["amount"]),
                "row_count": row.get("row_count"),
            })
        if len(buckets) < 2:
            raise PythonToolError("分层结果不足两层，无法进行分层分析。")
        if len({item["bucket"] for item in buckets}) != len(buckets):
            raise PythonToolError("分层编号重复，无法分析。")
        sum_buckets = sum((item["amount"] for item in buckets), Decimal(0))
        if sum_buckets.quantize(Decimal("0.01")) != total.quantize(Decimal("0.01")):
            raise PythonToolError(
                f"各分层合计 {sum_buckets} 与独立总额 {total} 不一致，不能输出分层结论。")
        if total == 0:
            raise PythonToolError("总额为 0，无法计算占比。")
        for item in buckets:
            item["share_percent"] = str(
                (item["amount"] / total * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            item["amount"] = str(item["amount"])
        return json.dumps({
            "operation": "decile", "source_round": request.source_round,
            "total_source_round": request.total_source_round,
            "result": {"buckets": buckets, "total": str(total), "sum_of_buckets": str(sum_buckets),
                       "reconciled": True, "caution": "分层描述分布构成，不证明因果。"},
        }, ensure_ascii=False)

    def _sequential_attribution(self, request: PythonAnalysisRequest,
                                detail_rows: list[dict], steps: list[AgentStep]) -> str:
        """Reuse the exact two-period reconciliation for every returned adjacent pair."""
        totals = self._rows_from_sql(request.total_source_round, steps)
        period = request.period_column
        dimension = request.dimension_column
        value = request.value_column
        if not 2 <= len(totals) <= 30 or len(detail_rows) > 100:
            raise PythonToolError("逐期归因支持 2 至 30 个时期、最多 100 行完整明细，请缩小范围。")
        if any(not {period, value}.issubset(row) or row[period] is None for row in totals):
            raise PythonToolError("独立总额缺少时期或数值列。")
        labels = [str(row[period]) for row in totals]
        if len(set(labels)) != len(labels) or labels != sorted(labels) or any(not label.strip() for label in labels):
            raise PythonToolError("独立总额必须按时期升序排列，每期恰好一行。")
        if any(not {period, dimension, value}.issubset(row) or row[period] is None
               for row in detail_rows):
            raise PythonToolError("维度明细缺少时期、维度或数值列。")
        if {str(row[period]) for row in detail_rows} != set(labels):
            raise PythonToolError("维度明细与独立总额的时期不一致，不能进行逐期归因。")
        comparisons = []
        for baseline, current in zip(labels, labels[1:]):
            pair = {baseline, current}
            pair_request = request.model_copy(update={"operation": "attribution",
                                                      "baseline_period": baseline, "current_period": current})
            result = json.loads(self._attribution(
                pair_request, [row for row in detail_rows if str(row[period]) in pair], steps,
                total_rows=[row for row in totals if str(row[period]) in pair],
            ))["result"]
            comparisons.append(result)
        return json.dumps({
            "operation": "sequential_attribution", "source_round": request.source_round,
            "total_source_round": request.total_source_round,
            "result": {"periods": labels, "comparisons": comparisons, "reconciled": True,
                       "caution": "比较的是本轮返回的相邻时期；未返回时期不能判为零或无数据。变化贡献不等于因果原因。"},
        }, ensure_ascii=False)

    @staticmethod
    def _decimal_value(value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PythonToolError("数值列包含非数值。") from exc
        if not amount.is_finite():
            raise PythonToolError("数值列不能包含无穷大或 NaN。")
        return amount
