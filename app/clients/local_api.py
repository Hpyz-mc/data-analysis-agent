"""Small server-side client for the existing FastAPI service."""

import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class APIError(Exception):
    pass


class DuplicateDatasetAPIError(APIError):
    def __init__(self, table_name: str, upload_time: str, message: str) -> None:
        self.table_name = table_name
        self.upload_time = upload_time
        super().__init__(message)


BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def _open_json(request: Request) -> dict:
    try:
        with urlopen(request, timeout=90) as response:
            return json.load(response) if response.status != 204 else {}
    except HTTPError as exc:
        try:
            detail = json.load(exc).get("detail", "请求失败")
        except (ValueError, UnicodeError):
            detail = "请求失败"
        if exc.code == 409 and isinstance(detail, dict):
            raise DuplicateDatasetAPIError(
                detail.get("table_name", "未知"), detail.get("upload_time", ""),
                detail.get("message", "检测到重复数据集。"),
            ) from exc
        if isinstance(detail, dict):
            detail = detail.get("message", "请求失败")
        raise APIError(f"接口返回 {exc.code}：{detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise APIError("无法连接分析服务，请检查 Docker 服务是否正在运行。") from exc


def _request(path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        BASE_URL + path,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method or ("POST" if payload is not None else "GET"),
    )
    return _open_json(request)


def analyze(question: str, session_id: str | None) -> dict:
    payload = {"question": question}
    if session_id:
        payload["session_id"] = session_id
    return _request("/api/v1/analyze", payload)


def analyze_stream(question: str, session_id: str | None):
    """Read application SSE events; a result event confirms persistence."""
    payload = {"question": question}
    if session_id:
        payload["session_id"] = session_id
    request = Request(BASE_URL + "/api/v1/analyze/stream",
                      data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Accept": "text/event-stream", "Content-Type": "application/json"})
    completed = False
    try:
        with urlopen(request, timeout=180) as response:
            event, data = "message", []
            for raw in response:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    value = json.loads("\n".join(data))
                    if event == "error":
                        raise APIError(str(value.get("message", "分析未完成，请重试。")))
                    if event == "result":
                        completed = True
                    yield event, value
                    event, data = "message", []
        if not completed:
            raise APIError("流式连接中断，回答未完成，请重试。")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise APIError("流式连接失败，请检查分析服务并重试。") from exc
    except (ValueError, UnicodeError) as exc:
        raise APIError("流式响应格式异常，请重试。") from exc


def get_history(session_id: str) -> dict:
    return _request(f"/api/v1/sessions/{session_id}")


def list_sessions() -> dict:
    return _request("/api/v1/sessions")


def delete_session(session_id: str) -> None:
    _request(f"/api/v1/sessions/{session_id}", method="DELETE")


def upload_dataset(file_bytes: bytes, filename: str, session_id: str | None) -> dict:
    boundary = f"codex-{uuid.uuid4().hex}"
    safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    parts = []
    if session_id:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"session_id\"\r\n\r\n{session_id}\r\n".encode())
    parts.extend((
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ))
    request = Request(
        BASE_URL + "/api/v1/datasets/upload", data=b"".join(parts), method="POST",
        headers={"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return _open_json(request)


def load_preset_dataset(preset_type: str = "ecommerce") -> dict:
    return _request(f"/api/v1/datasets/preset?preset_type={quote(preset_type)}", payload={})


def get_chart(path: str) -> bytes:
    """Only fetch chart paths served by this application, never arbitrary URLs."""
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise APIError("图表地址无效。")
    if not path.startswith("/api/v1/charts/") or ".." in path.split("/"):
        raise APIError("图表地址无效。")
    try:
        with urlopen(BASE_URL + path, timeout=15) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise APIError("图表加载失败，请检查服务状态。") from exc
