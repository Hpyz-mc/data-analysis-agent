"""Resolve a single business source and guard physical SQL sources, not CTE aliases."""

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from app.agent.scoped_analysis import METRIC_ALIASES, ScopeError, schema_columns
from app.agent.answer_contract import ALIASES


def normalize_source(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"(?:public\.)?[^\W\d]\w*", value, re.UNICODE):
        raise ScopeError("请选择 public schema 中可访问的数据表。")
    return value if value.startswith("public.") else "public." + value


def mentioned_sources(question: str, schema: str) -> set[str]:
    sources = {"public." + name for name in re.findall(r"\bpublic\.([^\W\d]\w*)", question)}
    for table in schema_columns(schema):
        name = table.removeprefix("public.")
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", question):
            sources.add(table)
    return sources


def resolve_source(question: str, schema: str, selected: str | None, previous=None) -> str | None:
    explicit = mentioned_sources(question, schema)
    source = normalize_source(selected) if selected else None
    if len(explicit) > 1 or (source and explicit and explicit != {source}):
        raise ScopeError("问题中指定的数据表与当前选择不一致，或涉及多张表。请确认本轮使用哪一张数据表。")
    source = source or next(iter(explicit), None)
    if source is None and previous is not None:
        source = normalize_source(previous.source_table)
    tables = schema_columns(schema)
    if source is None and len(tables) == 1:
        source = next(iter(tables))
    if source is not None and source not in tables:
        raise ScopeError(f"数据表 {source} 不在可访问的业务表中，请重新选择。")
    return source


def sql_source_error(sql: str, source: str | None) -> str | None:
    if source is None:
        return None
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        physical = []
        for scope in traverse_scope(tree):
            for _, (_, relation) in scope.selected_sources.items():
                if isinstance(relation, exp.Table):
                    physical.append(relation)
        if not physical:
            return "数据查询必须读取本轮选择的数据表，不能用常量代替查询。"
        for table in physical:
            actual = f"{table.db or 'public'}.{table.name}"
            if table.catalog or actual != source:
                return f"本轮只允许查询 {source}，SQL 引用了其他数据源 {actual}。"
    except (SqlglotError, ValueError):
        return "SQL 数据源无法验证，请提交仅使用本轮选中表的有效查询。"
    return None


def missing_field_error(question: str, schema: str, source: str | None) -> str | None:
    """Check explicit column requests, not broad business subjects or derived metrics."""
    if source is None or re.search(r"什么是|如何理解|定义|解释.*概念", question):
        return None
    columns = schema_columns(schema).get(source, {})
    available = set(columns)
    # Keep existing known labels (e.g. sales_amount → 销售额); never guess a refund mapping.
    for label, aliases in ALIASES.items():
        if aliases & available:
            available.add(label)
    available.update(label for label, aliases in METRIC_ALIASES.items() if set(aliases) & set(columns))
    requested = set(re.findall(r"(?:没有|缺少|不存在)\s*[`\"“]?([\w]+?)[`\"”]?字段", question))
    requested.update(re.findall(r"(?:字段|列名)\s*[`\"“]([\w]+)[`\"”]", question))
    # Plain business labels are not necessarily literal columns (销售额 may mean 订单金额).
    # Leave those to scope planning; this gate only handles explicit field references.
    requested -= {"这个", "这些", "该", "相关", "相应", "对应", "所需"}
    missing = sorted(requested - available)
    if missing:
        return (f"当前表没有字段 {'、'.join(missing)}，无法按该字段查询，不能用其他指标替代。"
                f"可用字段：{'、'.join(columns)}。请确认字段名或选择包含该字段的数据表。")
    return None
