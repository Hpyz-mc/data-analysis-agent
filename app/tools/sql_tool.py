import json
import re

import psycopg
from psycopg.rows import dict_row

from app.core.config import get_settings
from app.tools.sql_validator import SQLValidationError, validate_read_only_sql


class SQLToolError(RuntimeError):
    """A database query could not be completed."""


AMBIGUOUS_TABLE_PREFIX = "我检测到数据库中存在多份相似的数据表："


def _stem_matches_question(stem: str, question: str) -> bool:
    """Match a dataset topic without letting unrelated version groups interrupt."""
    stem, question = stem.casefold(), question.casefold()
    if stem in question:
        return True
    aliases = {
        "sales": ("销售", "营收"),
        "orders": ("订单",),
        "ecommerce": ("电商",),
    }
    for token in stem.split("_"):
        if len(token) < 2 or token.isdigit() or token in {"data", "dataset", "file", "table"}:
            continue
        if token in question or any(alias in question for alias in aliases.get(token, ())):
            return True
    return False


class SQLTool:
    """Run bounded, read-only queries against PostgreSQL."""

    def __init__(self, max_rows: int = 100, timeout_ms: int = 5000) -> None:
        self.database_url = get_settings().database_url
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms

    def _connect(self) -> psycopg.Connection:
        try:
            conn = psycopg.connect(
                self.database_url,
                connect_timeout=5,
                row_factory=dict_row,
            )
            conn.read_only = True
            return conn
        except psycopg.Error as exc:
            raise SQLToolError("无法连接 PostgreSQL，请检查 DATABASE_URL 和数据库状态。") from exc

    def get_schema(self) -> str:
        """Describe tables the database user can SELECT in the public schema."""
        query = """
            SELECT c.table_name, c.column_name, c.data_type
            FROM information_schema.columns AS c
            WHERE c.table_schema = 'public'
              AND c.table_name NOT IN
                  ('system_upload_records', 'analysis_sessions', 'analysis_turns')
              AND has_table_privilege(
                    current_user,
                    format('%I.%I', c.table_schema, c.table_name),
                    'SELECT'
              )
            ORDER BY c.table_name, c.ordinal_position
            LIMIT 300
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(self.timeout_ms),),
                    )
                    cur.execute(query)
                    columns = cur.fetchall()
                    if not columns:
                        cur.execute(
                            "SELECT current_database() AS database_name, "
                            "current_user AS role_name"
                        )
                        identity = cur.fetchone()
                        server = f"{conn.info.host}:{conn.info.port}"
        except psycopg.Error as exc:
            raise SQLToolError("读取数据库表结构失败。") from exc

        if not columns:
            raise SQLToolError(
                f"程序连接到 {server}/"
                f"{identity['database_name']}（账号 {identity['role_name']}），"
                "但没有找到 public schema 中可查询的表。"
                "请对照你导入数据时使用的数据库和账号。"
            )

        tables: dict[str, list[str]] = {}
        for column in columns:
            tables.setdefault(column["table_name"], []).append(
                f"{column['column_name']} ({column['data_type']})"
            )
        return "\n".join(
            f"public.{table}: {', '.join(fields)}"
            for table, fields in tables.items()
        )

    def find_ambiguous_imports(self, question: str) -> str | None:
        """Ask for a version when several imports share a normalized filename."""
        if re.search(r"什么是|解释.*概念|如何理解|定义", question):
            return None
        if re.search(
            r"(?:public\.)?(?:user_[\w]+_\d{8}_\d{2}|(?:demo|agent)_[a-z0-9_]+)",
            question, re.IGNORECASE,
        ):
            return None
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.system_upload_records') AS audit_table")
                    if cur.fetchone()["audit_table"] is None:
                        return None
                    cur.execute("""SELECT table_name, upload_time
                        FROM public.system_upload_records
                        WHERE table_name LIKE 'user_%'
                          AND CASE
                              WHEN to_regclass(format('public.%I', table_name)) IS NULL
                                  THEN FALSE
                              ELSE has_table_privilege(current_user,
                                  format('public.%I', table_name), 'SELECT')
                          END
                        ORDER BY upload_time DESC""")
                    records = cur.fetchall()
        except psycopg.Error as exc:
            raise SQLToolError("读取导入数据版本信息失败。") from exc
        groups: dict[str, list[dict]] = {}
        for record in records:
            matched = re.fullmatch(r"user_(.+)_\d{8}_\d{2}", record["table_name"])
            if matched:
                groups.setdefault(matched.group(1), []).append(record)
        for stem, versions in groups.items():
            if len(versions) < 2 or not _stem_matches_question(stem, question):
                continue
            entries = "\n".join(
                f"{index}. {item['table_name']}，上传时间 {item['upload_time'].isoformat()}"
                for index, item in enumerate(versions, 1)
            )
            return f"{AMBIGUOUS_TABLE_PREFIX}\n{entries}\n请问您当前这轮分析需要使用哪一份数据？"
        return None

    def run(self, sql: str) -> str:
        try:
            safe_sql = validate_read_only_sql(sql)
        except SQLValidationError as exc:
            raise SQLToolError(str(exc)) from exc

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(self.timeout_ms),),
                    )
                    cur.execute("SET LOCAL search_path = pg_catalog, public")
                    cur.execute(safe_sql)
                    rows = cur.fetchmany(self.max_rows + 1)
        except psycopg.Error as exc:
            raise SQLToolError(f"SQL 执行失败：{exc.diag.message_primary or '数据库错误'}") from exc

        result = {
            "rows": rows[: self.max_rows],
            "row_count": min(len(rows), self.max_rows),
            "truncated": len(rows) > self.max_rows,
        }
        return json.dumps(result, ensure_ascii=False, default=str)
