import logging
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from app.api.analysis import router as analysis_router
from app.api.analysis import AnalysisFailure
from app.tools.chart_tool import CHART_DIR


logger = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="数据分析 Agent",
    description="从最小 Agent Loop 开始构建的数据分析 Agent。",
    version="0.5.0-dev",
)


@app.exception_handler(AnalysisFailure)
async def analysis_failure_response(request: Request, exc: AnalysisFailure):
    return JSONResponse(status_code=exc.status_code, content={
        "detail": exc.detail, "usage": exc.usage.model_dump() if exc.usage else None,
        "diagnostics": exc.diagnostics.model_dump() if exc.diagnostics else None,
    })


@app.middleware("http")
async def log_request(request: Request, call_next):
    """Log request outcome without logging questions, SQL, or credentials."""
    request_id = uuid4().hex
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_failed request_id=%s method=%s path=%s",
            request_id,
            request.method,
            request.url.path,
        )
        raise
    elapsed_ms = round((perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_complete request_id=%s method=%s path=%s status=%s elapsed_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


app.include_router(analysis_router)
app.mount(
    "/api/v1/charts",
    StaticFiles(directory=CHART_DIR, check_dir=False),
    name="charts",
)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
