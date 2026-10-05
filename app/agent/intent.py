"""Conservative time-grain intent checks, independent of model wording."""
import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


def requested_time_grain(question: str) -> str | None:
    patterns = {
        "day": r"按日|按天|每日|每天|日度|逐日",
        "week": r"按周|每周|周度|逐周",
        "month": r"按月|每月|每个(?:月|月份)|逐月|月度|各月|各个月|各月份",
        "quarter": r"按季度|每季度|每个季度|各季度|季度走势",
        "year": r"按年|每年|每个年度|各年|年度走势",
    }
    matched = [grain for grain, pattern in patterns.items() if re.search(pattern, question)]
    return matched[0] if len(matched) == 1 else None


def _grain_expression(expression, grain):
    rendered = expression.sql(dialect="postgres")
    if re.search(r"DATE_TRUNC\s*\(\s*'" + grain + r"'", rendered, re.I):
        return True
    formats = {"day": "YYYY-MM-DD", "week": "IYYY-IW", "month": "YYYY-MM",
               "quarter": 'YYYY-"Q"Q', "year": "YYYY"}
    if re.search(r"TO_CHAR\s*\(.*'" + re.escape(formats[grain]) + r"'", rendered, re.I):
        return True
    # EXTRACT forms need the year as well; otherwise multiple years are merged.
    return False


def query_matches_grain(sql: str, grain: str) -> bool:
    try:
        query = sqlglot.parse_one(sql, read="postgres")
    except SqlglotError:
        return False
    if not isinstance(query, exp.Select) or query.args.get("group") is None:
        return False
    selected = query.expressions
    aliases = {item.alias: item.this for item in selected if isinstance(item, exp.Alias)}
    for expression in query.args["group"].expressions:
        if isinstance(expression, exp.Literal) and expression.is_int:
            index = int(expression.this) - 1
            if not 0 <= index < len(selected):
                continue
            expression = selected[index]
        elif isinstance(expression, exp.Column) and expression.name in aliases:
            expression = aliases[expression.name]
        if _grain_expression(expression, grain):
            return True
    return False


def grain_feedback(question, steps) -> str:
    grain = requested_time_grain(question)
    if grain is None:
        return ""
    labels = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}
    for step in steps:
        if step.action != "sql" or not query_matches_grain(step.tool_input, grain):
            continue
        try:
            result = json.loads(step.observation)
        except (ValueError, TypeError):
            continue
        if isinstance(result, dict) and isinstance(result.get("rows"), list) and not result.get("truncated"):
            return ""
    return (f"用户要求按{labels[grain]}分组，当前查询尚未取得对应粒度的数据，"
            "不能用范围合计代替。请使用 DATE_TRUNC 或明确年月标签进行分组查询。")
