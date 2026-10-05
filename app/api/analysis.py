from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from app.llm.usage import AnalysisUsage
from app.agent.diagnostics import AnalysisDiagnostics

from app.agent.loop import DataAnalysisAgent
from app.agent.analysis_policy import ANALYSIS_CLARIFICATION_PREFIX
from app.core.config import ConfigurationError, get_settings
from app.llm.deepseek import DeepSeekClient, LLMError
from app.schemas.analysis import (
    AnalysisRequest,
    AnalysisResponse,
    SessionListResponse,
    SessionHistoryResponse,
)
from app.session.store import (
    SessionBusyError,
    SessionNotFoundError,
    SessionStorageError,
    SessionStore,
)
from app.tools.python_tool import PythonAnalysisTool
from app.tools.chart_tool import ChartTool
from app.tools.sql_tool import AMBIGUOUS_TABLE_PREFIX, SQLTool, SQLToolError
from app.tools.dataset_importer import (
    MAX_FILE_BYTES, DatasetImporter, DatasetImportError, DuplicateDatasetError,
)


router = APIRouter(prefix="/api/v1", tags=["analysis"])


class AnalysisFailure(HTTPException):
    def __init__(self, detail: str, usage: AnalysisUsage | None,
                 diagnostics: AnalysisDiagnostics | None = None, status_code: int = 502):
        super().__init__(status_code=status_code, detail=detail)
        self.usage = usage
        self.diagnostics = diagnostics


def _diagnostics(agent):
    snapshot = getattr(agent, "diagnostics_snapshot", None)
    value = snapshot() if callable(snapshot) else None
    return value if isinstance(value, AnalysisDiagnostics) else None


def _importer() -> DatasetImporter:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not settings.session_database_url:
        raise HTTPException(status_code=503, detail="数据导入未启用，请配置 SESSION_DATABASE_URL。")
    return DatasetImporter(settings.session_database_url, settings.database_url)


@router.post("/datasets/upload")
def upload_dataset(file: UploadFile = File(...), session_id: UUID | None = Form(None)) -> dict:
    """Import one CSV or Excel file through the platform writer account."""
    try:
        data = file.file.read(MAX_FILE_BYTES + 1)
        return _importer().import_file(data, file.filename or "", session_id)
    except DuplicateDatasetError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "table_name": exc.table_name,
            "upload_time": exc.upload_time.isoformat(),
        }) from exc
    except DatasetImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        file.file.close()


@router.get("/datasets")
def list_datasets() -> dict:
    """List registered datasets; preview at most three rows per table."""
    try:
        return {"datasets": _importer().list_datasets()}
    except DatasetImportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/datasets/preset")
def load_preset_dataset(preset_type: str = "ecommerce") -> dict:
    """Create or reuse the Q1 2026 ecommerce demonstration table."""
    try:
        return _importer().load_preset(preset_type)
    except DatasetImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/analyze", response_model=AnalysisResponse)
def analyze(request: AnalysisRequest) -> AnalysisResponse:
    """Run one independent analysis turn, with history when sessions are configured."""
    return _analyze(request)


@router.post("/analyze/stream")
def analyze_stream(request: AnalysisRequest):
    """SSE events: session (when enabled), status, delta, result (persisted), or error."""
    from app.api.streaming import analysis_events
    return StreamingResponse(analysis_events(lambda emit: _analyze(request, emit)),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _analyze(request: AnalysisRequest, emit=None) -> AnalysisResponse:
    try:
        settings = get_settings()
        agent = DataAnalysisAgent(
            llm=DeepSeekClient(),
            sql_tool=SQLTool(),
            python_tool=PythonAnalysisTool(),
            chart_tool=ChartTool(),
        )
        if not settings.session_database_url:
            if request.session_id is not None:
                raise SessionStorageError("会话未启用，请先配置 SESSION_DATABASE_URL。")
            return agent.run(question=request.question, **({"emit": emit} if emit else {}))

        store = SessionStore(settings.session_database_url)
        with store.begin_turn(request.session_id, request.question) as turn:
            context = getattr(turn, "analysis_context", None)
            try:
                if emit:
                    # Keep a new conversation recoverable even if the final stream fails.
                    emit("session", {"session_id": str(turn.session_id)})
                response = agent.run(
                    question=request.question, conversation_history=turn.history,
                    **({"analysis_context": context} if context is not None else {}),
                    **({"emit": emit} if emit else {}),
                )
            except Exception:
                usage = agent.llm.usage_snapshot()
                diagnostics = _diagnostics(agent)
                turn.finish(None, "failed", artifacts={
                    "usage": usage.model_dump() if isinstance(usage, AnalysisUsage) else None,
                    "diagnostics": diagnostics.model_dump() if isinstance(diagnostics, AnalysisDiagnostics) else None,
                })
                raise
            usable = _usable_for_history(response)
            response.context_reset = bool(
                usable and response.finished and response.plan.requires_sql
                and context is not None and response.analysis_context is None
            )
            memory_context = response.analysis_context
            if memory_context and memory_context.kind == "lookup" and context and context.kind != "lookup":
                memory_context = context  # A quick lookup does not erase the ongoing comparison.
            response.session_context = None if response.context_reset else memory_context or context
            artifacts = {
                "usage": response.usage.model_dump() if response.usage else None,
                "charts": response.charts,
                "suggestions": [item.model_dump() for item in response.suggestions],
                "analysis_context": response.analysis_context.model_dump(mode="json") if response.analysis_context else None,
                "context_reset": response.context_reset,
                "session_context": response.session_context.model_dump(mode="json") if response.session_context else None,
                "diagnostics": response.diagnostics.model_dump() if response.diagnostics else None,
            }
            turn.finish(
                response.answer,
                "completed" if usable else "incomplete",
                **({"artifacts": artifacts, "analysis_context": memory_context,
                    "context_reset": response.context_reset}
                   if response.usage or response.charts or response.analysis_context or response.context_reset or response.session_context or response.diagnostics else {}),
            )
            response.session_id = turn.session_id
            return response
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SessionStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except LLMError as exc:
        usage = agent.llm.usage_snapshot()
        raise AnalysisFailure(str(exc), usage if isinstance(usage, AnalysisUsage) else None,
                              _diagnostics(agent)) from exc
    except SQLToolError as exc:
        raise AnalysisFailure(str(exc), None, _diagnostics(agent),
                              status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from exc


def _usable_for_history(response: AnalysisResponse) -> bool:
    """Keep evidence-backed answers and genuine clarification turns for follow-ups."""
    if response.requires_clarification and not response.steps and not response.charts:
        return True
    if (
        not response.finished
        and response.plan is None
        and response.rounds == 0
        and not response.steps
        and not response.charts
        and (
            response.answer.startswith(ANALYSIS_CLARIFICATION_PREFIX)
            or (
                response.answer.startswith(f"{AMBIGUOUS_TABLE_PREFIX}\n")
                and response.answer.endswith("请问您当前这轮分析需要使用哪一份数据？")
            )
        )
    ):
        return True
    if not response.finished or response.plan is None:
        return False
    if response.plan.requires_sql:
        sql_steps = [step for step in response.steps if step.action == "sql"]
        if not any(
            DataAnalysisAgent._has_query_result(step)
            and not DataAnalysisAgent._is_truncated_sql_result(step)
            for step in sql_steps
        ):
            return False
    if response.plan.python_operation and not any(
        DataAnalysisAgent._has_tool_result(step, response.plan.python_operation)
        for step in response.steps
    ):
        return False
    if response.plan.chart_type and not any(
        DataAnalysisAgent._has_tool_result(step, response.plan.chart_type)
        for step in response.steps
    ):
        return False
    return True


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions() -> SessionListResponse:
    """List up to 20 most recently active local-development conversations."""
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not settings.session_database_url:
        raise HTTPException(status_code=503, detail="会话未启用，请先配置 SESSION_DATABASE_URL。")
    try:
        sessions = SessionStore(settings.session_database_url).list_sessions(limit=20)
    except SessionStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return SessionListResponse(sessions=sessions)


@router.get("/sessions/{session_id}", response_model=SessionHistoryResponse)
def get_session(session_id: UUID) -> SessionHistoryResponse:
    """Read recent turns for a local-development conversation."""
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not settings.session_database_url:
        raise HTTPException(status_code=503, detail="会话未启用，请先配置 SESSION_DATABASE_URL。")
    try:
        turns = SessionStore(settings.session_database_url).get_history(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return SessionHistoryResponse(session_id=session_id, turns=turns)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: UUID) -> None:
    """Permanently remove a local-development session and all its turns."""
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not settings.session_database_url:
        raise HTTPException(status_code=503, detail="会话未启用，请先配置 SESSION_DATABASE_URL。")
    try:
        SessionStore(settings.session_database_url).delete_session(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SessionStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
