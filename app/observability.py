import json
import logging
import re
import uuid
from collections.abc import Iterable
from contextvars import ContextVar
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = b"x-request-id"
# Client-supplied request ids are echoed into the response header and every log
# line, so restrict them to a safe, bounded charset (defense-in-depth: do not rely
# on the ASGI server to reject CRLF / oversized values).
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")

# Per-request correlation id. Set by RequestIdMiddleware in the same task that
# runs the endpoint, so a sync endpoint (run in the anyio threadpool, which copies
# the context) and its log records observe the same id.
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    return _request_id_ctx.get()


class JsonLogFormatter(logging.Formatter):
    """Structured single-line JSON logs, each tagged with the request id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "request_id": _request_id_ctx.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    # Idempotent: install one JSON StreamHandler on the root logger. Additive (no
    # handlers.clear()) so a test runner's own log-capture handlers are preserved.
    root = logging.getLogger()
    if any(isinstance(handler.formatter, JsonLogFormatter) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class RequestIdMiddleware:
    # Pure ASGI middleware (not BaseHTTPMiddleware) so the ContextVar is set in the
    # same task that runs the endpoint and therefore reaches its log records.
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._incoming_request_id(scope) or uuid.uuid4().hex
        token = _request_id_ctx.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _request_id_ctx.reset(token)

    @staticmethod
    def _incoming_request_id(scope: Scope) -> str | None:
        headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", [])
        for name, value in headers:
            if name == REQUEST_ID_HEADER:
                candidate = value.decode("latin-1").strip()
                # Reject CRLF, spaces, oversized or exotic values; a rejected id
                # falls through to a freshly generated uuid4 instead of being echoed.
                if _REQUEST_ID_RE.fullmatch(candidate):
                    return candidate
                return None
        return None


class MaxBodySizeMiddleware:
    # Reject oversized request bodies with HTTP 413 before the whole payload is
    # buffered into memory. Enforced two ways so a missing, chunked, or dishonest
    # Content-Length cannot bypass the limit:
    #   1. fast path - reject immediately when a declared Content-Length exceeds the
    #      limit, without reading the body;
    #   2. streaming guard - otherwise read the body counting actual bytes and reject
    #      as soon as the running total exceeds the limit (covers Transfer-Encoding:
    #      chunked, a missing Content-Length, or one that lies). At most max_bytes is
    #      buffered, then replayed to the application.
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(send)
            return

        # Buffer the body up to the limit, counting actual bytes, then replay it.
        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                buffered.append(message)
                break
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await self._reject(send)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        replayed = iter(buffered)

        async def replay() -> Message:
            try:
                return next(replayed)
            except StopIteration:
                return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", [])
        for name, value in headers:
            if name == b"content-length":
                try:
                    return int(value.decode("ascii"))
                except ValueError:
                    # A malformed Content-Length is hostile/ambiguous: do not trust it
                    # for a fast reject, but do not fail open either - fall through to
                    # the streaming guard, which counts the real bytes.
                    return None
        return None

    @staticmethod
    async def _reject(send: Send) -> None:
        start: Message = {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
        body: Message = {
            "type": "http.response.body",
            "body": b'{"detail":"Request body too large."}',
        }
        await send(start)
        await send(body)
