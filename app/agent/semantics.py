"""Conservative column semantics for known and user-imported datasets."""

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


@dataclass(frozen=True)
class ColumnSemantic:
    unit: str
    grain: str
    aggregation: str


DEMO_COLUMNS = {
    "sales_amount": ColumnSemantic("金额", "月份×渠道×品类", "可按互斥分组求和"),
    "order_count": ColumnSemantic("订单数", "月份×渠道×品类", "可按互斥分组求和"),
    "stockout_days": ColumnSemantic("天", "月份×渠道×品类", "不可跨品类直接求和为自然日"),
}

POTENTIALLY_NON_ADDITIVE = re.compile(
    r"(?:rate|ratio|percent|percentage|days|duration|_avg|率|占比|天数|时长|均值)",
    re.IGNORECASE,
)


def semantic_notes(schema: str) -> str:
    notes: list[str] = []
    for line in schema.splitlines():
        if ": " not in line:
            continue
        table, fields = line.split(": ", 1)
        if table == "public.demo_ecommerce_orders":
            measures = "; ".join(
                f"{name}（{item.unit}，粒度：{item.grain}）：{item.aggregation}"
                for name, item in DEMO_COLUMNS.items()
            )
            notes.append(
                f"demo_ecommerce_orders 的已知字段语义：{measures}。"
                "stockout_days 在不同品类行可能重复记录同一段断货时间，"
                "禁止 SUM 后称为实际自然日天数。"
            )
        elif table.startswith("public.user_"):
            columns = [name for name in re.findall(r"([^,\s]+)\s+\([^)]+\)", fields)
                       if POTENTIALLY_NON_ADDITIVE.search(name)]
            if columns:
                notes.append(
                    f"{table} 的 {', '.join(columns)} 可能是比率、均值或持续时间；"
                    "导入时未确认业务粒度，不要直接求和，需询问口径或注明限制。"
                )
    return "\n".join(notes)


def semantic_sql_error(statement: str) -> str | None:
    """Block a known invalid aggregation; unknown imports receive a warning only."""
    try:
        parsed = sqlglot.parse_one(statement, read="postgres")
    except ParseError:
        return None  # The read-only SQL validator reports syntax errors separately.
    if parsed is None or not any(
        table.name.lower() == "demo_ecommerce_orders"
        for table in parsed.find_all(exp.Table)
    ):
        return None
    if any(
        column.name.lower() == "stockout_days"
        for aggregate in parsed.find_all(exp.Sum)
        for column in aggregate.find_all(exp.Column)
    ):
        return (
            "demo_ecommerce_orders.stockout_days 在不同品类行可能重复，"
            "不能 SUM 后解释为实际断货自然日。请按渠道/月份保留粒度，"
            "或使用 MAX 并说明它仅是该分组的记录值。"
        )
    return None
