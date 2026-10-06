"""PostgreSQL storage for conversations, isolated from the read-only SQL tool."""

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.agent.state import ConversationTurn
from app.agent.context import AnalysisContext


logger = logging.getLogger("uvicorn.error")
TurnStatus = Literal["completed", "incomplete", "failed"]


def _display_question(question: str) -> str:
    """Remove the UI's source hint for labels, without changing stored evidence."""
    cleaned = re.sub(r"^使用数据表 public\.\w+。\s*", "", question.strip(), count=1)
    return re.sub(r"\s+", " ", cleaned or question.strip())


class SessionStorageError(RuntimeError):
    """Session storage is unavailable or a write failed."""


class SessionNotFoundError(RuntimeError):
    """The requested session does not exist."""


class SessionBusyError(RuntimeError):
    """Another request is currently running for this session."""


def _bounded_history(rows: list[dict], max_chars: int = 6000) -> list[ConversationTurn]:
    """Keep the most recent completed turns within a prompt budget."""
    selected: list[ConversationTurn] = []
    remaining = max_chars
    for row in rows:  # newest first
        question = row["question"][:800]
        answer = row["answer"][:1200]
        needed = len(question) + len(answer)
        if needed > remaining:
            if selected:
                break
            answer = answer[: max(0, remaining - len(question))]
        selected.append(ConversationTurn(question=question, answer=answer))
        remaining -= len(question) + len(answer)
    selected.reverse()
    return selected


@dataclass
class ActiveTurn:
    connection: psycopg.Connection
    session_id: UUID
    turn_no: int
    history: list[ConversationTurn]
    done: bool = False
    analysis_context: AnalysisContext | None = None

    def finish(self, answer: str | None, status: TurnStatus, *,
               artifacts: dict | None = None, analysis_context: AnalysisContext | None = None,
               context_reset: bool = False) -> None:
        if self.done:
            raise SessionStorageError("该轮对话已经保存，不能重复结束。")
        try:
            with self.connection.transaction():
                with self.connection.cursor() as cur:
                    cur.execute(
                        """UPDATE public.analysis_turns
                           SET answer = %s, status = %s, artifacts = %s, finished_at = now()
                           WHERE turn_no = %s AND status = 'running'""",
                        (answer, status, Jsonb(artifacts or {}), self.turn_no),
                    )
                    if cur.rowcount != 1:
                        raise SessionStorageError("当前对话状态已改变，无法保存结果。")
                    cur.execute(
                        "UPDATE public.analysis_sessions SET updated_at = now() WHERE id = %s",
                        (self.session_id,),
                    )
                    if analysis_context is not None and status == "completed":
                        cur.execute(
                            "UPDATE public.analysis_sessions SET analysis_context = %s WHERE id = %s",
                            (Jsonb(analysis_context.model_dump(mode="json")), self.session_id),
                        )
                    elif context_reset and status == "completed":
                        cur.execute(
                            "UPDATE public.analysis_sessions SET analysis_context = NULL WHERE id = %s",
                            (self.session_id,),
                        )
        except psycopg.Error as exc:
            raise SessionStorageError("保存对话失败，请稍后重试。") from exc
        self.done = True


class SessionStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(
                self.database_url,
                connect_timeout=5,
                autocommit=True,
                row_factory=dict_row,
            )
        except psycopg.Error as exc:
            raise SessionStorageError("无法连接会话数据库，请检查会话账号和数据库状态。") from exc

    @contextmanager
    def begin_turn(self, session_id: UUID | None, question: str) -> Iterator[ActiveTurn]:
        """Serialize work per session; stale running turns become interrupted."""
        selected_id = session_id or uuid4()
        connection = self._connect()
        try:
            lock_key = int.from_bytes(selected_id.bytes[:8], "big", signed=True)
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))
                    if not cur.fetchone()["acquired"]:
                        raise SessionBusyError("这段对话正在处理上一条问题，请稍后再试。")
                with connection.transaction():
                    with connection.cursor() as cur:
                        if session_id is None:
                            cur.execute(
                                "INSERT INTO public.analysis_sessions (id) VALUES (%s)",
                                (selected_id,),
                            )
                        else:
                            cur.execute(
                                "SELECT id, analysis_context FROM public.analysis_sessions WHERE id = %s",
                                (selected_id,),
                            )
                            session = cur.fetchone()
                            if session is None:
                                raise SessionNotFoundError("找不到这段对话。")
                        cur.execute(
                            """UPDATE public.analysis_turns
                               SET status = 'interrupted', finished_at = now()
                               WHERE session_id = %s AND status = 'running'""",
                            (selected_id,),
                        )
                        cur.execute(
                            """SELECT question, answer
                               FROM public.analysis_turns
                               WHERE session_id = %s AND status = 'completed'
                               ORDER BY turn_no DESC LIMIT 5""",
                            (selected_id,),
                        )
                        history = _bounded_history(cur.fetchall())
                        cur.execute(
                            """INSERT INTO public.analysis_turns
                               (session_id, question, status)
                               VALUES (%s, %s, 'running') RETURNING turn_no""",
                            (selected_id, question),
                        )
                        turn_no = cur.fetchone()["turn_no"]
            except psycopg.Error as exc:
                raise SessionStorageError("读取或创建对话失败，请检查会话表和账号权限。") from exc

            stored_context = session.get("analysis_context") if session_id is not None else None
            active = ActiveTurn(
                connection, selected_id, turn_no, history,
                analysis_context=AnalysisContext.model_validate(stored_context) if stored_context else None,
            )
            try:
                yield active
            finally:
                if not active.done:
                    try:
                        active.finish(None, "failed")
                    except SessionStorageError:
                        logger.exception("session_turn_failed_to_save_status")
        finally:
            connection.close()  # PostgreSQL releases the advisory lock here.

    def get_history(self, session_id: UUID, limit: int | None = 20) -> list[dict]:
        connection = self._connect()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT id FROM public.analysis_sessions WHERE id = %s",
                    (session_id,),
                )
                if cur.fetchone() is None:
                    raise SessionNotFoundError("找不到这段对话。")
                if limit is None:
                    cur.execute(
                        """SELECT turn_no, question, answer, status, created_at, finished_at, artifacts
                           FROM public.analysis_turns
                           WHERE session_id = %s ORDER BY turn_no""",
                        (session_id,),
                    )
                    return [self._history_artifacts(row) for row in cur.fetchall()]
                cur.execute(
                    """SELECT turn_no, question, answer, status, created_at, finished_at, artifacts
                       FROM public.analysis_turns
                       WHERE session_id = %s
                       ORDER BY turn_no DESC LIMIT %s""",
                    (session_id, limit),
                )
                return [self._history_artifacts(row) for row in reversed(cur.fetchall())]
        except psycopg.Error as exc:
            raise SessionStorageError("读取对话历史失败，请稍后重试。") from exc
        finally:
            connection.close()

    @staticmethod
    def _history_artifacts(row: dict) -> dict:
        artifacts = row.pop("artifacts", None) or {}
        return {**row, "usage": artifacts.get("usage"), "charts": artifacts.get("charts", []),
                "suggestions": artifacts.get("suggestions", []),
                "analysis_context": artifacts.get("analysis_context"),
                "context_reset": artifacts.get("context_reset", False),
                "session_context": artifacts.get("session_context"),
                "diagnostics": artifacts.get("diagnostics")}

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List recent conversations with titles from their first question."""
        connection = self._connect()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """SELECT s.id AS session_id, first_turn.question AS first_question,
                              s.created_at, s.updated_at
                       FROM public.analysis_sessions AS s
                       JOIN LATERAL (
                           SELECT question
                           FROM public.analysis_turns
                           WHERE session_id = s.id
                           ORDER BY turn_no ASC
                           LIMIT 1
                       ) AS first_turn ON TRUE
                       ORDER BY s.updated_at DESC, s.created_at DESC, s.id DESC
                       LIMIT %s""",
                    (limit,),
                )
                return [
                    {
                        "session_id": row["session_id"],
                        "title": _display_question(row["first_question"])[:15],
                        "question_preview": _display_question(row["first_question"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                    for row in cur.fetchall()
                ]
        except psycopg.Error as exc:
            raise SessionStorageError("读取会话列表失败，请稍后重试。") from exc
        finally:
            connection.close()

    def delete_all_sessions(self) -> None:
        """Delete every session and all turns atomically, unless one is in use."""
        connection = self._connect()
        try:
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(9223372036854775806) AS acquired")
                    if not cur.fetchone()["acquired"]:
                        raise SessionBusyError("正在处理上一条问题，请稍后再试。")
                with connection.transaction():
                    with connection.cursor() as cur:
                        cur.execute("DELETE FROM public.analysis_turns")
                        cur.execute("DELETE FROM public.analysis_sessions")
            except psycopg.Error as exc:
                raise SessionStorageError("清空会话失败，请检查会话账号权限。") from exc
        finally:
            connection.close()

    def delete_session(self, session_id: UUID) -> None:
        """Delete a session and all its turns atomically, unless it is in use."""
        connection = self._connect()
        try:
            lock_key = int.from_bytes(session_id.bytes[:8], "big", signed=True)
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))
                    if not cur.fetchone()["acquired"]:
                        raise SessionBusyError("这段对话正在处理上一条问题，请稍后再试。")
                with connection.transaction():
                    with connection.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM public.analysis_sessions WHERE id = %s FOR UPDATE",
                            (session_id,),
                        )
                        if cur.fetchone() is None:
                            raise SessionNotFoundError("找不到这段对话。")
                        cur.execute(
                            "DELETE FROM public.analysis_turns WHERE session_id = %s",
                            (session_id,),
                        )
                        cur.execute(
                            "DELETE FROM public.analysis_sessions WHERE id = %s",
                            (session_id,),
                        )
            except psycopg.Error as exc:
                raise SessionStorageError("删除会话失败，请检查会话账号权限。") from exc
        finally:
            connection.close()
