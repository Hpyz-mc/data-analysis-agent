import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError


class SQLValidationError(ValueError):
    """The model generated SQL outside the read-only query policy."""


FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Into,
)

# Keep this intentionally narrow. SELECT alone is not safe: XML export and
# dynamic-query functions can read tables hidden inside string arguments.
ALLOWED_FUNCTIONS = frozenset({
    "SUM", "COUNT", "AVG", "MIN", "MAX", "CORR", "ARRAY_AGG", "STDDEV", "STDDEV_POP",
    "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP", "PERCENTILE_CONT",
    "PERCENTILE_DISC", "ABS", "ROUND", "TRUNC", "FLOOR", "CEIL", "CEILING",
    "POWER", "SQRT", "LN", "LOG", "EXP", "MOD", "COALESCE", "NULLIF",
    "GREATEST", "LEAST", "DATE_TRUNC", "DATE_PART", "TO_CHAR", "TO_DATE",
    "TO_TIMESTAMP", "LOWER", "UPPER", "LENGTH", "CHAR_LENGTH", "TRIM",
    "BTRIM", "LTRIM", "RTRIM", "SUBSTRING", "REPLACE", "CONCAT",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD",
    "FIRST_VALUE", "LAST_VALUE", "GENERATE_SERIES",
})
# sqlglot normalizes PostgreSQL syntax/functions to these internal names.
ALLOWED_NORMALIZED_FUNCTIONS = ALLOWED_FUNCTIONS | {
    "CAST", "CASE", "IF", "AND", "OR", "EXISTS", "EXTRACT", "TIMESTAMP_TRUNC", "TIME_TO_STR",
    "STR_TO_DATE", "STR_TO_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP",
}


def _validate_functions(expression: exp.Expression) -> None:
    for function in expression.find_all(exp.Func):
        if isinstance(function.parent, exp.Dot):
            namespace = function.parent.this
            if (not isinstance(namespace, exp.Identifier)
                    or namespace.name != "pg_catalog"):
                raise SQLValidationError("SQL 不允许调用业务 schema 或外部 schema 中的函数。")
        if isinstance(function, exp.Anonymous):
            name, allowed = function.name.upper(), ALLOWED_FUNCTIONS
        else:
            name, allowed = function.sql_name(), ALLOWED_NORMALIZED_FUNCTIONS
        if name not in allowed:
            raise SQLValidationError(f"SQL 函数 {name} 不在允许的分析函数列表中。")


def validate_read_only_sql(sql: str) -> str:
    statement = sql.strip()
    if not statement or len(statement) > 10_000:
        raise SQLValidationError("SQL 不能为空且不能超过 10000 个字符。")

    try:
        parsed = sqlglot.parse(
            statement,
            read="postgres",
            error_level=ErrorLevel.RAISE,
        )
    except ParseError as exc:
        raise SQLValidationError("SQL 语法解析失败。") from exc

    if len(parsed) != 1 or parsed[0] is None:
        raise SQLValidationError("每次只能执行一条 SQL。")

    expression = parsed[0]
    if not isinstance(expression, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        raise SQLValidationError("SQL Tool 只允许 SELECT 查询。")
    if any(isinstance(node, FORBIDDEN_NODES) for node in expression.walk()):
        raise SQLValidationError("SQL 包含写入或管理数据库的操作。")
    internal_tables = {"system_upload_records", "analysis_sessions", "analysis_turns"}
    if any(table.name.lower() in internal_tables for table in expression.find_all(exp.Table)):
        raise SQLValidationError("SQL 不允许访问平台内部管理表。")
    _validate_functions(expression)

    return statement
