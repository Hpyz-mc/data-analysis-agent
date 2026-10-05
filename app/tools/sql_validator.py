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

    return statement
