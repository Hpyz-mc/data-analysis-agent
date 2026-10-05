"""Transactional, versioned ingestion through the platform writer account."""

import hashlib
import io
import json
import re
import time
import unicodedata
from zipfile import BadZipFile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from psycopg import sql
from psycopg.rows import dict_row


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 200
DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d")
NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


class DatasetImportError(ValueError):
    """A file is invalid or the database cannot safely import it."""


class DuplicateDatasetError(DatasetImportError):
    def __init__(self, table_name: str, upload_time: datetime) -> None:
        self.table_name = table_name
        self.upload_time = upload_time
        super().__init__(
            f"检测到内容完全相同的文件已存在（对应数据表：{table_name}，"
            f"上传时间：{upload_time.isoformat()}），无需重复导入。"
        )


def _limit_identifier(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore").rstrip("_")


def _clean_identifier(
    value: str, *, fallback: str, max_length: int, ascii_only: bool = False,
) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if ascii_only:
        normalized = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^a-z0-9]+" if ascii_only else r"[^\w]+", "_", normalized.lower()).strip("_")
    if cleaned and cleaned[0].isdigit():
        cleaned = fallback if cleaned.isdigit() else f"{fallback}_{cleaned}"
    return _limit_identifier(cleaned or fallback, max_length)


def _clean_columns(columns: list) -> list[str]:
    names: list[str] = []
    used: set[str] = set()
    for index, original in enumerate(columns, 1):
        base = _clean_identifier(str(original).strip(), fallback=f"column_{index}", max_length=54)
        name = base
        suffix = 2
        while name in used:
            tag = f"_{suffix}"
            name = f"{_limit_identifier(base, 54 - len(tag))}{tag}"
            suffix += 1
        used.add(name)
        names.append(name)
    return names


def _parse_file(file_bytes: bytes, filename: str) -> tuple[pd.DataFrame, bool]:
    if not file_bytes:
        raise DatasetImportError("文件为空，请选择包含表头和数据行的文件。")
    if len(file_bytes) > MAX_FILE_BYTES:
        raise DatasetImportError("文件超过 20MB 限制，请缩小后重新上传。")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls"}:
        raise DatasetImportError("仅支持 .csv、.xlsx 和 .xls 文件。")
    try:
        if suffix == ".csv":
            frame = None
            for encoding in ("utf-8-sig", "gbk"):
                try:
                    frame = pd.read_csv(
                        io.BytesIO(file_bytes), encoding=encoding, dtype=str,
                        nrows=MAX_ROWS + 1, keep_default_na=False,
                    )
                    break
                except UnicodeDecodeError:
                    continue
            if frame is None:
                raise DatasetImportError("CSV 编码无法识别；请使用 UTF-8 或 GBK。")
        else:
            frame = pd.read_excel(
                io.BytesIO(file_bytes), dtype=str, nrows=MAX_ROWS + 1,
                keep_default_na=False, engine="openpyxl" if suffix == ".xlsx" else "xlrd",
            )
    except DatasetImportError:
        raise
    except (ValueError, OSError, BadZipFile, pd.errors.ParserError, ImportError) as exc:
        raise DatasetImportError("文件格式或内容无法解析，请检查表头和数据行。") from exc
    if frame.empty:
        raise DatasetImportError("文件没有可导入的数据行。")
    if len(frame.columns) > MAX_COLUMNS:
        raise DatasetImportError("列数超过 200 列限制，请精简字段。")
    frame.columns = _clean_columns(list(frame.columns))
    truncated = len(frame) > MAX_ROWS
    return frame.head(MAX_ROWS).copy(), truncated


def _convert_column(values: list) -> tuple[str, list]:
    cleaned = [None if pd.isna(v) or str(v).strip() == "" else str(v).strip() for v in values]
    non_null = [v for v in cleaned if v is not None]
    if not non_null:
        return "TEXT", cleaned
    numbers = []
    try:
        for value in non_null:
            token = value.replace("￥", "").replace("¥", "").replace("$", "").strip()
            if not NUMBER_PATTERN.fullmatch(token):
                raise ValueError
            numbers.append(Decimal(token.replace(",", "")))
        iterator = iter(numbers)
        return "NUMERIC", [next(iterator) if value is not None else None for value in cleaned]
    except (ValueError, InvalidOperation):
        pass
    parsed_dates = []
    try:
        for value in non_null:
            parsed_dates.append(next(datetime.strptime(value, fmt).date() for fmt in DATE_PATTERNS
                                     if _matches_date(value, fmt)))
        iterator = iter(parsed_dates)
        return "DATE", [next(iterator) if value is not None else None for value in cleaned]
    except (ValueError, StopIteration):
        return "TEXT", cleaned


def _matches_date(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def _prepare_frame(frame: pd.DataFrame) -> tuple[list[str], list[str], list[tuple]]:
    columns = list(frame.columns)
    converted = [_convert_column(frame[name].tolist()) for name in columns]
    types = [kind for kind, _ in converted]
    rows = list(zip(*(values for _, values in converted)))
    return columns, types, rows


def _preview(columns: list[str], rows: list[tuple]) -> list[dict]:
    return [
        {name: (value.isoformat() if isinstance(value, date) else
                str(value) if isinstance(value, Decimal) else value)
         for name, value in zip(columns, row)}
        for row in rows[:3]
    ]


class DatasetImporter:
    def __init__(self, session_database_url: str, database_url: str) -> None:
        self.session_database_url = session_database_url
        self.database_url = database_url

    def _connect(self, url: str, *, read_only: bool = False) -> psycopg.Connection:
        for attempt in range(2):
            try:
                conn = psycopg.connect(url, connect_timeout=5, row_factory=dict_row)
                if read_only:
                    conn.read_only = True
                return conn
            except psycopg.OperationalError as exc:
                if attempt:
                    raise DatasetImportError("数据库连接超时或不可用，请稍后重试。") from exc
                time.sleep(0.3)
        raise AssertionError("unreachable")

    def _readonly_role(self) -> str:
        try:
            with self._connect(self.database_url, read_only=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT current_user AS role_name")
                    return cur.fetchone()["role_name"]
        except psycopg.Error as exc:
            raise DatasetImportError("无法确认分析只读账号，请检查 DATABASE_URL。") from exc

    def list_datasets(self) -> list[dict]:
        """Read registered datasets and a bounded preview through the reader role."""
        try:
            with self._connect(self.database_url, read_only=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '5s'")
                    cur.execute("SELECT to_regclass('public.system_upload_records') AS existing")
                    if cur.fetchone()["existing"] is None:
                        return []
                    cur.execute("""SELECT table_name, original_file_name, row_count AS rows, upload_time
                        FROM public.system_upload_records
                        WHERE to_regclass('public.' || quote_ident(table_name)) IS NOT NULL
                          AND has_table_privilege(current_user, to_regclass('public.' || quote_ident(table_name)), 'SELECT')
                        ORDER BY upload_time DESC, id DESC LIMIT 20""")
                    records = cur.fetchall()
                    for record in records:
                        name = record["table_name"]
                        cur.execute("""SELECT column_name FROM information_schema.columns
                            WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (name,))
                        record["columns"] = [row["column_name"] for row in cur.fetchall()]
                        cur.execute(sql.SQL("SELECT * FROM {} LIMIT 3").format(sql.Identifier("public", name)))
                        record["preview"] = cur.fetchall()
                    return records
        except psycopg.Error as exc:
            raise DatasetImportError("暂时无法读取数据集，请检查数据库状态与只读权限。") from exc

    @staticmethod
    def _ensure_audit(cur, reader_role: str) -> None:
        cur.execute("""CREATE TABLE IF NOT EXISTS public.system_upload_records (
            id SERIAL PRIMARY KEY,
            table_name VARCHAR(128) NOT NULL UNIQUE,
            original_file_name VARCHAR(256) NOT NULL,
            file_hash CHAR(64) NOT NULL,
            row_count INT NOT NULL,
            session_id UUID,
            upload_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_upload_records_hash "
                    "ON public.system_upload_records(file_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_upload_records_session "
                    "ON public.system_upload_records(session_id)")
        cur.execute(sql.SQL("GRANT SELECT ON TABLE public.system_upload_records TO {}")
                    .format(sql.Identifier(reader_role)))

    @staticmethod
    def _next_name(cur, stem: str) -> str:
        prefix = f"user_{stem}_{datetime.now(ZoneInfo('Asia/Shanghai')):%Y%m%d}_"
        for seq in range(1, 100):
            name = f"{prefix}{seq:02d}"
            cur.execute("""SELECT to_regclass(%s) AS existing,
                EXISTS(SELECT 1 FROM public.system_upload_records WHERE table_name = %s)
                AS audited""", (f"public.{name}", name))
            result = cur.fetchone()
            if result["existing"] is None and not result["audited"]:
                return name
        raise DatasetImportError("当天同名数据集版本已满，请更换文件名。")

    def import_file(self, file_bytes: bytes, filename: str, session_id: UUID | None = None) -> dict:
        frame, truncated = _parse_file(file_bytes, filename)
        columns, types, rows = _prepare_frame(frame)
        digest = hashlib.sha256(file_bytes).hexdigest()
        original_name = Path(filename).name[:256]
        stem = _clean_identifier(
            Path(filename).stem, fallback="dataset", max_length=35, ascii_only=True,
        )
        reader_role = self._readonly_role()
        try:
            with self._connect(self.session_database_url) as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_xact_lock(%s)",
                                    (int.from_bytes(bytes.fromhex(digest[:16]), "big", signed=True),))
                        stem_key = hashlib.sha256(stem.encode()).digest()[:8]
                        cur.execute("SELECT pg_advisory_xact_lock(%s)",
                                    (int.from_bytes(stem_key, "big", signed=True),))
                        self._ensure_audit(cur, reader_role)
                        cur.execute("SELECT table_name, upload_time FROM public.system_upload_records "
                                    "WHERE file_hash = %s LIMIT 1", (digest,))
                        existing = cur.fetchone()
                        if existing:
                            raise DuplicateDatasetError(existing["table_name"], existing["upload_time"])
                        name = self._next_name(cur, stem)
                        fields = sql.SQL(", ").join(
                            sql.SQL("{} {}").format(sql.Identifier(column), sql.SQL(kind))
                            for column, kind in zip(columns, types)
                        )
                        cur.execute(sql.SQL("CREATE TABLE {} ({})").format(
                            sql.Identifier("public", name), fields))
                        insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                            sql.Identifier("public", name),
                            sql.SQL(", ").join(map(sql.Identifier, columns)),
                            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                        )
                        cur.executemany(insert, rows)
                        cur.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                            sql.Identifier("public", name), sql.Identifier(reader_role)))
                        cur.execute("""INSERT INTO public.system_upload_records
                            (table_name, original_file_name, file_hash, row_count, session_id)
                            VALUES (%s, %s, %s, %s, %s)""",
                            (name, original_name, digest, len(rows), session_id))
        except DuplicateDatasetError:
            raise
        except psycopg.Error as exc:
            raise DatasetImportError("数据导入失败：请检查写入账号的建表/授权权限及数据库状态。") from exc
        return {"status": "success", "table_name": name, "rows": len(rows),
                "columns": columns, "preview": _preview(columns, rows), "truncated": truncated}

    def load_preset(self, preset_type: str = "ecommerce") -> dict:
        if preset_type != "ecommerce":
            raise DatasetImportError("暂只支持 ecommerce 官方演示数据。")
        reader_role = self._readonly_role()
        name = "demo_ecommerce_orders"
        rows = []
        totals = {
            "线上商城": [("电脑", 168000), ("家电", 92000), ("服饰", 46000)],
            "线下门店": [("电脑", 110000), ("家电", 60000), ("服饰", 30000)],
            "第三方平台": [("电脑", 74000), ("家电", 51000), ("服饰", 35000)],
        }
        factors = {1: Decimal("1"), 2: Decimal("0.815"), 3: Decimal("0.95")}
        for month in (1, 2, 3):
            for channel, categories in totals.items():
                for category, base in categories:
                    factor = factors[month] if channel == "线下门店" else Decimal("1") + Decimal(month - 1) / 20
                    amount = Decimal(base) * factor
                    rows.append((date(2026, month, 15), channel, category, amount,
                                 int(amount // 500), 8 if channel == "线下门店" and month == 2 else 0,
                                 "供应链断货" if channel == "线下门店" and month == 2 else "正常"))
        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str).encode()).hexdigest()
        try:
            with self._connect(self.session_database_url) as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_xact_lock(%s)", (193873644390017,))
                        self._ensure_audit(cur, reader_role)
                        cur.execute("SELECT to_regclass(%s) AS existing", (f"public.{name}",))
                        exists = cur.fetchone()["existing"] is not None
                        if not exists:
                            cur.execute("""CREATE TABLE public.demo_ecommerce_orders (
                                order_date DATE NOT NULL, channel TEXT NOT NULL,
                                category TEXT NOT NULL, sales_amount NUMERIC NOT NULL,
                                order_count INT NOT NULL, stockout_days INT NOT NULL,
                                supply_status TEXT NOT NULL)""")
                            cur.executemany("""INSERT INTO public.demo_ecommerce_orders
                                (order_date, channel, category, sales_amount, order_count,
                                 stockout_days, supply_status)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)""", rows)
                            cur.execute("""INSERT INTO public.system_upload_records
                                (table_name, original_file_name, file_hash, row_count)
                                VALUES (%s, %s, %s, %s)""",
                                (name, "official_ecommerce_2026_q1", digest, len(rows)))
                        else:
                            cur.execute("SELECT file_hash FROM public.system_upload_records WHERE table_name = %s", (name,))
                            record = cur.fetchone()
                            if record is None or record["file_hash"].strip() != digest:
                                raise DatasetImportError("演示表名已被其他数据占用，未覆盖原表。")
                        cur.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                            sql.Identifier("public", name), sql.Identifier(reader_role)))
        except psycopg.Error as exc:
            raise DatasetImportError("演示数据创建失败，请检查数据库权限及状态。") from exc
        return {"status": "success", "table_name": name, "rows": len(rows),
                "columns": ["order_date", "channel", "category", "sales_amount", "order_count",
                            "stockout_days", "supply_status"],
                "preview": _preview(["order_date", "channel", "category", "sales_amount",
                                     "order_count", "stockout_days", "supply_status"], rows),
                "existing": exists}
