"""Bounded SSE bridge for the synchronous analysis worker."""
import json
from queue import Queue, Empty, Full
from threading import Event, Thread

from fastapi import HTTPException
from app.llm.deepseek import LLMError


class StreamCancelled(LLMError):
    def __init__(self, message):
        super().__init__(message, code="stream_cancelled")


def analysis_events(work):
    queue = Queue(maxsize=64)
    cancelled = Event()

    def emit(event, data):
        while not cancelled.is_set():
            try:
                queue.put((event, data), timeout=0.5)
                return
            except Full:
                continue
        raise StreamCancelled("流式连接已关闭，本轮分析未完成。")

    def run():
        try:
            result = work(emit)
            emit("result", result.model_dump(mode="json"))
        except StreamCancelled:
            return
        except HTTPException as exc:
            usage = getattr(exc, "usage", None)
            diagnostics = getattr(exc, "diagnostics", None)
            try:
                emit("error", {"message": exc.detail, "status": exc.status_code,
                               "usage": usage.model_dump() if usage else None,
                               "diagnostics": diagnostics.model_dump() if diagnostics else None})
            except StreamCancelled:
                pass
        except Exception:
            import logging
            logging.getLogger("uvicorn.error").exception("analysis_stream_failed")
            try:
                emit("error", {"message": "分析服务异常，请稍后重试。", "status": 500})
            except StreamCancelled:
                pass

    Thread(target=run, daemon=True).start()
    try:
        while True:
            try:
                event, data = queue.get(timeout=10)
            except Empty:
                yield ": heartbeat\n\n"
                continue
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if event in {"result", "error"}:
                return
    finally:
        cancelled.set()
