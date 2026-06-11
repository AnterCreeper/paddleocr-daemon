#!/usr/bin/env python3
"""PaddleOCR API Daemon — HTTP wrapper around PaddleOCRVL pipeline.

Accepts PDF / images via REST API and delegates to the local layout-detection
model + a remote VLM backend (llama.cpp / vLLM / OpenAI-compatible).

Endpoints
---------
GET  /health
POST /layout-parsing   {"file": "<base64>", "fileType": 0|1, ...}
"""

from __future__ import annotations

import base64
import hmac
import inspect
import io
import json
import os
import select
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Configuration (environment variables)
# ---------------------------------------------------------------------------
VLM_BACKEND = os.environ.get("VLM_BACKEND", "llama-cpp-server")
VLM_SERVER_URL = os.environ.get("VLM_SERVER_URL", "http://127.0.0.1:3000/v1")
VLM_API_KEY = os.environ.get("VLM_API_KEY", "no-key")
PIPELINE_VERSION = os.environ.get("PIPELINE_VERSION", "v1.6")
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8080"))
API_TOKEN = os.environ.get("API_TOKEN", "")
TMP_DIR = os.environ.get("TMP_DIR", "/tmp/paddleocr")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/root/output")
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", str(32 * 1024 * 1024)))
PREDICT_TIMEOUT = int(os.environ.get("PREDICT_TIMEOUT", "600"))
READY_CACHE_TTL = float(os.environ.get("READY_CACHE_TTL", "5"))

Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Processing error — HTTP status is separate from PaddleOCR errorCode
# ---------------------------------------------------------------------------
class ProcessingError(Exception):
    """Known processing error with HTTP status and PaddleOCR error code."""

    def __init__(self, http_status: int, msg: str, error_code: int = 1):
        self.http_status = http_status
        self.error_code = error_code
        super().__init__(msg)


class ClientDisconnected(Exception):
    """Raised when the HTTP client closes the connection before completion."""

    def __init__(self, cancelled: bool):
        self.cancelled = cancelled
        super().__init__("client disconnected")


# ---------------------------------------------------------------------------
# Lazy pipeline (loaded on first request to keep startup fast)
# ---------------------------------------------------------------------------
_pipeline = None
_pipeline_lock = threading.Lock()
_predict_executor = ThreadPoolExecutor(max_workers=1)
_ready_cache_lock = threading.Lock()
_ready_cache: tuple[float, bool, str] | None = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                from paddleocr import PaddleOCRVL

                sys.stderr.write(
                    f"[daemon] Initialising PaddleOCRVL "
                    f"(version={PIPELINE_VERSION}, backend={VLM_BACKEND}, "
                    f"server={VLM_SERVER_URL})...\n"
                )
                _pipeline = PaddleOCRVL(
                    pipeline_version=PIPELINE_VERSION,
                    vl_rec_backend=VLM_BACKEND,
                    vl_rec_server_url=VLM_SERVER_URL,
                    vl_rec_api_key=VLM_API_KEY,
                )
                sys.stderr.write("[daemon] Pipeline ready.\n")
    return _pipeline


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------
def _get_result_field(result: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(result, dict) and key in result:
            return result[key]
        if result is None:
            continue
        try:
            inspect.getattr_static(result, key)
        except AttributeError:
            continue
        else:
            return getattr(result, key)
    return None


def _encode_image_map(image_map: Any) -> dict[str, str]:
    images: dict[str, str] = {}
    if not isinstance(image_map, dict):
        return images

    for name, data in image_map.items():
        if data is None:
            continue
        if isinstance(data, bytes):
            images[str(name)] = base64.b64encode(data).decode("ascii")
        elif hasattr(data, "save"):
            buffer = io.BytesIO()
            data.save(buffer, format="PNG")
            images[str(name)] = base64.b64encode(buffer.getvalue()).decode("ascii")
    return images


def _collect_markdown_images(predict_result) -> dict[str, str]:
    """Extract {filename: base64} image map from a PaddleOCR result page."""
    md_result = _get_result_field(predict_result, "markdown")
    if md_result is None:
        return {}

    # PaddleOCR-VL local results often expose image assets as
    # `markdown_images`, while some wrappers/project docs refer to `images`.
    image_map = _get_result_field(md_result, "markdown_images", "images")
    return _encode_image_map(image_map)


def _extract_markdown_text(predict_result) -> str:
    md_result = _get_result_field(predict_result, "markdown")
    if md_result is None:
        return ""

    for key in ("markdown_texts", "text"):
        value = _get_result_field(md_result, key)
        if value is not None:
            return str(value)

    return ""


def _result_to_response(log_id: str, output) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for res in output:
        page: dict[str, Any] = {
            "markdown": {
                "text": _extract_markdown_text(res),
                "images": _collect_markdown_images(res),
            },
        }
        for field in ("prunedResult", "outputImages", "inputImage", "exports"):
            value = _get_result_field(res, field)
            if value is not None:
                page[field] = value
        pages.append(page)

    return {
        "logId": log_id,
        "errorCode": 0,
        "errorMsg": "Success",
        "result": {"layoutParsingResults": pages},
    }


def _error_response(log_id: str, code: int, msg: str) -> dict[str, Any]:
    return {
        "logId": log_id,
        "errorCode": code,
        "errorMsg": msg,
        "result": {"layoutParsingResults": []},
    }


def _normalize_file_type(value: Any) -> int | None:
    """Accept PaddleOCR-style file type values while ignoring extra fields."""
    if value is None:
        return 1

    if isinstance(value, bool):
        return None  # bool is a subclass of int; must check before int below

    if isinstance(value, int):
        return value if value in (0, 1) else None

    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"0", "pdf", "application/pdf"}:
            return 0
        if v in {"1", "image", "img", "picture"}:
            return 1

    return None


def _extract_base64_payload(value: Any) -> str:
    """Accept either raw base64 or a data URL payload."""
    if not isinstance(value, str):
        return ""

    payload = value.strip()
    if payload.startswith("data:") and "," in payload:
        _, payload = payload.split(",", 1)
    return payload.strip()


def _is_authorized(headers) -> bool:
    if not API_TOKEN:
        return True

    auth_header = headers.get("Authorization", "")
    expected = f"Bearer {API_TOKEN}"
    return hmac.compare_digest(auth_header, expected)


def _is_vlm_backend_reachable() -> tuple[bool, str]:
    """Best-effort readiness check for remote VLM backends."""
    if not VLM_SERVER_URL:
        return False, "missing VLM_SERVER_URL"

    parsed = urlparse(VLM_SERVER_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, f"invalid VLM_SERVER_URL: {VLM_SERVER_URL}"

    probe_url = VLM_SERVER_URL.rstrip("/") + "/models"
    headers = {}
    if VLM_API_KEY:
        headers["Authorization"] = f"Bearer {VLM_API_KEY}"

    try:
        req = Request(probe_url, headers=headers, method="GET")
        with urlopen(req, timeout=5) as resp:
            status = getattr(resp, "status", 200)
            if not (200 <= status < 300):
                return False, f"unexpected status from VLM backend: {status}"

            payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return False, "invalid /models response: expected JSON object"

            models = payload.get("data")
            if models is None and isinstance(payload.get("models"), list):
                models = payload.get("models")
            if not isinstance(models, list) or not models:
                return False, "invalid /models response: missing non-empty data list"

            first = models[0]
            if not isinstance(first, dict):
                return False, "invalid /models response: model entries must be objects"

            if not any(key in first for key in ("id", "model", "name")):
                return False, "invalid /models response: model entry missing id/model/name"

            return True, f"reachable ({status}), {len(models)} model(s) listed"
    except Exception as exc:
        return False, f"vlm backend probe failed: {type(exc).__name__}: {exc}"


def _is_vlm_backend_ready_cached() -> tuple[bool, str]:
    """Cache readiness probes so frequent health checks do not hammer VLM."""
    global _ready_cache

    now = time.monotonic()
    with _ready_cache_lock:
        if _ready_cache is not None:
            cached_at, ok, detail = _ready_cache
            if now - cached_at < READY_CACHE_TTL:
                return ok, f"{detail} (cached)"

    ok, detail = _is_vlm_backend_reachable()
    with _ready_cache_lock:
        _ready_cache = (time.monotonic(), ok, detail)
    return ok, detail


def _get_request_length(headers) -> int:
    try:
        length = int(headers.get("Content-Length", "0") or "0")
    except ValueError as exc:
        raise ValueError(f"invalid Content-Length: {exc}") from exc

    if length <= 0:
        raise ValueError("empty body")
    if length > MAX_REQUEST_BYTES:
        raise ValueError(
            f"request too large: {length} bytes exceeds limit {MAX_REQUEST_BYTES}"
        )
    return length


def _validate_raw_size(raw: bytes) -> None:
    size = len(raw)
    if size == 0:
        raise ValueError("empty file payload")
    if size > MAX_REQUEST_BYTES:
        raise ValueError(
            f"decoded file too large: {size} bytes exceeds limit {MAX_REQUEST_BYTES}"
        )


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "PaddleOCR-Daemon/1.0"

    def do_GET(self) -> None:
        started_at = time.monotonic()
        path = self.path.rstrip("/")

        if path == "/health":
            self._handle_health(started_at)
            return
        if path in {"/ready", "/health/ready"}:
            self._handle_ready(started_at)
            return
        self._send_json(404, {"error": "not found"})
        self._log_event("request_rejected", path=path or "/", status=404,
                        durationMs=int((time.monotonic() - started_at) * 1000),
                        error="not found")

    def _handle_health(self, started_at: float) -> None:
        log_id = uuid.uuid4().hex[:12]
        self._send_json(200, {
            "logId": log_id,
            "errorCode": 0,
            "errorMsg": "Healthy",
        })
        self._log_event("health_check", path="/health", status=200,
                        durationMs=int((time.monotonic() - started_at) * 1000))

    def _handle_ready(self, started_at: float) -> None:
        log_id = uuid.uuid4().hex[:12]
        vlm_ok, vlm_detail = _is_vlm_backend_ready_cached()
        if not vlm_ok:
            self._send_json(503, {
                "logId": log_id,
                "errorCode": 1,
                "errorMsg": "Service unavailable",
            })
            self._log_event("readiness_check", path="/ready", status=503,
                            durationMs=int((time.monotonic() - started_at) * 1000),
                            error=vlm_detail)
            return

        self._send_json(200, {
            "logId": log_id,
            "errorCode": 0,
            "errorMsg": "Ready",
        })
        self._log_event("readiness_check", path="/ready", status=200,
                        durationMs=int((time.monotonic() - started_at) * 1000))

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        started_at = time.monotonic()

        if not _is_authorized(self.headers):
            self._send_json(401, {"error": "unauthorized"})
            self._log_event("request_rejected", path=path or "/", status=401, durationMs=int((time.monotonic() - started_at) * 1000), error="unauthorized")
            return

        if path == "/layout-parsing":
            self._handle_layout_parsing()
        else:
            self._send_json(404, {"error": "not found"})
            self._log_event("request_rejected", path=path or "/", status=404, durationMs=int((time.monotonic() - started_at) * 1000), error="not found")

    # ------------------------------------------------------------------
    # POST /layout-parsing  (official PaddleOCR-compatible API)
    # ------------------------------------------------------------------
    def _reject_parse(self, log_id: str, started_at: float, code: int, msg: str) -> None:
        self._send_json(400, _error_response(log_id, code, msg))
        self._log_request(log_id, "/layout-parsing", 400, started_at, error=msg)

    def _handle_layout_parsing(self) -> None:
        log_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()

        try:
            length = _get_request_length(self.headers)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            self._reject_parse(log_id, started_at, 1, f"invalid JSON: {exc}")
            return

        file_data = _extract_base64_payload(body.get("file", ""))
        if not file_data:
            self._reject_parse(log_id, started_at, 1, "missing 'file' field")
            return

        file_type = _normalize_file_type(body.get("fileType"))
        if file_type is None:
            self._reject_parse(log_id, started_at, 1, "invalid 'fileType'; expected 0 or 1")
            return

        sys.stderr.write(f"[daemon] [{log_id}] request fileType={file_type} size={len(file_data)}\n")

        try:
            result = self._process_file(log_id, file_data, file_type)
            self._send_json(200, result)
            self._log_request(log_id, "/layout-parsing", 200, started_at)
        except ClientDisconnected as exc:
            self._log_request(
                log_id,
                "/layout-parsing",
                499,
                started_at,
                error=f"client disconnected; cancelled={exc.cancelled}",
            )
        except ProcessingError as exc:
            self._send_json(exc.http_status, _error_response(log_id, exc.error_code, str(exc)))
            self._log_request(log_id, "/layout-parsing", exc.http_status, started_at, error=str(exc))
        except Exception as exc:
            tb = traceback.format_exc()
            sys.stderr.write(f"[daemon] [{log_id}] ERROR: {tb}\n")
            self._send_json(500, _error_response(log_id, 2, f"{type(exc).__name__}: {exc}"))
            self._log_request(log_id, "/layout-parsing", 500, started_at, error=str(exc))

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------
    def _process_file(self, log_id: str, file_data: str, file_type: int) -> dict[str, Any]:
        """Decode base64 input to a temp file, run pipeline, return response."""
        try:
            raw = base64.b64decode(file_data, validate=True)
        except Exception:
            raise ProcessingError(400, "invalid base64 in 'file' field")

        try:
            _validate_raw_size(raw)
        except ValueError as exc:
            raise ProcessingError(400, str(exc))

        suffix = ".pdf" if file_type == 0 else ".png"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=TMP_DIR)
        keep_tmp_path = False
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)

            pipeline = _get_pipeline()

            sys.stderr.write(f"[daemon] [{log_id}] running pipeline on {tmp_path}\n")
            try:
                future = _predict_executor.submit(pipeline.predict, tmp_path)
                output = self._wait_for_predict(log_id, future, tmp_path)
            except ClientDisconnected as exc:
                if not exc.cancelled:
                    keep_tmp_path = True
                raise
            except FutureTimeoutError:
                if not future.cancel():
                    keep_tmp_path = True
                    sys.stderr.write(
                        f"[daemon] [{log_id}] timed out; keeping temp file "
                        f"for still-running pipeline: {tmp_path}\n"
                    )
                raise ProcessingError(
                    504,
                    f"pipeline predict timed out after {PREDICT_TIMEOUT}s",
                    error_code=2,
                )
            sys.stderr.write(f"[daemon] [{log_id}] pipeline returned {len(output)} pages\n")

            result = _result_to_response(log_id, output)
            return result
        finally:
            if not keep_tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _wait_for_predict(self, log_id: str, future, tmp_path: str):
        deadline = time.monotonic() + PREDICT_TIMEOUT
        done_reader, done_writer = socket.socketpair()

        def _notify_done(_future) -> None:
            try:
                done_writer.send(b"1")
            except OSError:
                pass

        future.add_done_callback(_notify_done)

        try:
            while True:
                if future.done():
                    return future.result()

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FutureTimeoutError()

                readable, _, _ = select.select(
                    [self.connection, done_reader], [], [], remaining
                )

                if done_reader in readable:
                    return future.result()

                if self.connection in readable:
                    try:
                        data = self.connection.recv(1, socket.MSG_PEEK)
                        disconnected = data == b""
                    except BlockingIOError:
                        disconnected = False
                    except (ConnectionError, OSError):
                        disconnected = True

                    if not disconnected:
                        continue

                    cancelled = future.cancel()
                    if not cancelled:
                        sys.stderr.write(
                            f"[daemon] [{log_id}] client disconnected; "
                            f"pipeline already running for {tmp_path}\n"
                        )
                    raise ClientDisconnected(cancelled=cancelled)
        finally:
            done_reader.close()
            done_writer.close()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {format % args}\n")

    def _log_request(
        self,
        log_id: str,
        path: str,
        status: int,
        started_at: float,
        error: str | None = None,
    ) -> None:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        self._log_event(
            "request_complete",
            logId=log_id,
            path=path,
            status=status,
            durationMs=duration_ms,
            error=error,
        )

    def _log_event(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "event": event,
            "ts": int(time.time()),
        }
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        sys.stderr.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    sys.stderr.write(f"[daemon] Starting on {API_HOST}:{API_PORT}\n")
    sys.stderr.write(f"[daemon] VLM backend: {VLM_BACKEND} @ {VLM_SERVER_URL}\n")
    sys.stderr.write(f"[daemon] Pipeline version: {PIPELINE_VERSION}\n")
    sys.stderr.write(f"[daemon] API auth: {'enabled' if API_TOKEN else 'disabled'}\n")

    httpd = ThreadingHTTPServer((API_HOST, API_PORT), Handler)

    def _shutdown_handler(signum: int, _frame) -> None:
        sys.stderr.write(f"[daemon] Received signal {signum}, shutting down...\n")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        httpd.serve_forever()
    finally:
        _predict_executor.shutdown(wait=False, cancel_futures=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
