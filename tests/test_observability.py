import asyncio
import json
import logging

from app.observability import (
    JsonLogFormatter,
    MaxBodySizeMiddleware,
    _request_id_ctx,
    get_request_id,
)
from starlette.types import Message, Receive, Scope, Send


def _record(msg: str = "hello %s", args: tuple[object, ...] = ("world",)) -> logging.LogRecord:
    return logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_get_request_id_defaults_to_dash() -> None:
    assert get_request_id() == "-"


def test_json_formatter_emits_structured_fields() -> None:
    payload = json.loads(JsonLogFormatter().format(_record()))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "-"
    assert "time" in payload


def test_json_formatter_includes_active_request_id() -> None:
    token = _request_id_ctx.set("abc123")
    try:
        payload = json.loads(JsonLogFormatter().format(_record()))
    finally:
        _request_id_ctx.reset(token)

    assert payload["request_id"] == "abc123"


async def _drive_max_body_middleware(
    content_length: bytes | None, body: bytes, max_bytes: int
) -> tuple[int, bool]:
    # Drive MaxBodySizeMiddleware directly over ASGI so the Content-Length header can
    # be set independently of the real body size (httpx would recompute it). Returns
    # the response status and whether the inner application was reached.
    headers: list[tuple[bytes, bytes]] = []
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope: Scope = {"type": "http", "headers": headers}

    first: Message = {"type": "http.request", "body": body, "more_body": False}
    disconnect: Message = {"type": "http.disconnect"}
    chunks = iter([first])

    async def receive() -> Message:
        try:
            return next(chunks)
        except StopIteration:
            return disconnect

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    inner_reached = False

    async def inner_app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal inner_reached
        inner_reached = True
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = MaxBodySizeMiddleware(inner_app, max_bytes=max_bytes)
    await middleware(scope, receive, send)

    status = next(int(m["status"]) for m in sent if m["type"] == "http.response.start")
    return status, inner_reached


def test_max_body_rejects_lying_content_length() -> None:
    # Declares a tiny Content-Length but streams a large body: the fast path cannot
    # catch it (declared <= max), so the streaming byte counter must reject -> 413.
    status, inner_reached = asyncio.run(
        _drive_max_body_middleware(content_length=b"10", body=b"x" * 5000, max_bytes=1000)
    )

    assert status == 413
    assert inner_reached is False


def test_max_body_rejects_malformed_content_length_when_body_too_large() -> None:
    # A non-numeric Content-Length must not fail open: it falls through to the
    # streaming guard, which counts real bytes and rejects an oversized body.
    status, inner_reached = asyncio.run(
        _drive_max_body_middleware(content_length=b"notanumber", body=b"x" * 5000, max_bytes=1000)
    )

    assert status == 413
    assert inner_reached is False


def test_max_body_allows_malformed_content_length_when_body_small() -> None:
    # The flip side: a malformed Content-Length must not over-reject a small body.
    status, inner_reached = asyncio.run(
        _drive_max_body_middleware(content_length=b"notanumber", body=b"x" * 100, max_bytes=1000)
    )

    assert status == 200
    assert inner_reached is True
