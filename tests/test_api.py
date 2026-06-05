import asyncio
import smtplib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from app.api.routes import (
    get_mail_sender,
    get_message_routing_service,
    get_ollama_client,
)
from app.config import get_settings
from app.infrastructure.ollama_client import (
    OllamaChatError,
    OllamaReadiness,
    OllamaUnavailableError,
)
from app.main import app, create_app
from app.schemas.messages import (
    MESSAGE_MAX_LENGTH,
    MessageRouteRequest,
    MessageRouteResponse,
)
from fastapi import FastAPI


def request_app(
    target_app: FastAPI,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    async def call_app() -> httpx.Response:
        transport = httpx.ASGITransport(app=target_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(call_app())


def request(method: str, url: str, **kwargs: object) -> httpx.Response:
    return request_app(app, method, url, **kwargs)


class FakeMessageRoutingService:
    def __init__(self, response: MessageRouteResponse | None = None) -> None:
        self.requests: list[MessageRouteRequest] = []
        self.response = response or MessageRouteResponse(
            status="sent",
            recipient="help-desk@example.com",
            reply_to="sender@example.com",
            subject="Message routed to help-desk",
            reason="The message concerns a computer issue.",
            used_fallback=False,
            model="qwen2.5:3b",
            routing_confidence="high",
        )

    def route_message(self, route_request: MessageRouteRequest) -> MessageRouteResponse:
        self.requests.append(route_request)
        return self.response


class RaisingMessageRoutingService:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def route_message(self, route_request: object) -> MessageRouteResponse:
        raise self.exc


def request_with_service(
    service: object,
    method: str,
    url: str,
    target_app: FastAPI = app,
    **kwargs: object,
) -> httpx.Response:
    target_app.dependency_overrides[get_message_routing_service] = lambda: service
    try:
        return request_app(target_app, method, url, **kwargs)
    finally:
        target_app.dependency_overrides.clear()


def test_api_app_imports() -> None:
    assert app.title == "Local Message Routing Agent"
    assert app.docs_url == "/api/v1/docs"
    assert app.redoc_url == "/api/v1/redoc"
    assert app.openapi_url == "/api/v1/openapi.json"


def test_create_app_uses_configured_app_name_and_api_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_NAME", "Configured App")
    monkeypatch.setenv("API_PREFIX", "/custom")

    try:
        configured_app = create_app()

        assert configured_app.title == "Configured App"
        assert configured_app.docs_url == "/custom/docs"
        assert configured_app.redoc_url == "/custom/redoc"
        assert configured_app.openapi_url == "/custom/openapi.json"

        response = request_with_service(
            FakeMessageRoutingService(),
            "POST",
            "/custom/messages/route",
            target_app=configured_app,
            json={"email": "sender@example.com", "message": "Hello"},
        )
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()


def test_health_endpoint_returns_ok() -> None:
    response = request("GET", "/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_docs_and_openapi_paths_are_available() -> None:
    docs_response = request("GET", "/api/v1/docs")
    redoc_response = request("GET", "/api/v1/redoc")
    openapi_response = request("GET", "/api/v1/openapi.json")

    assert docs_response.status_code == 200
    assert "text/html" in docs_response.headers["content-type"]
    assert redoc_response.status_code == 200
    assert "text/html" in redoc_response.headers["content-type"]
    assert openapi_response.status_code == 200
    assert openapi_response.json()["info"]["title"] == "Local Message Routing Agent"
    # Default unprefixed docs endpoints are not exposed, including the Swagger
    # OAuth2 redirect helper, which FastAPI would otherwise register at the unprefixed
    # /docs/oauth2-redirect regardless of docs_url. It is disabled (no OAuth flow in
    # this PoC), so neither the unprefixed nor a prefixed variant exists.
    assert request("GET", "/docs").status_code == 404
    assert request("GET", "/redoc").status_code == 404
    assert request("GET", "/docs/oauth2-redirect").status_code == 404
    assert request("GET", "/api/v1/docs/oauth2-redirect").status_code == 404


def test_openapi_message_route_schemas_include_examples() -> None:
    response = request("GET", "/api/v1/openapi.json")
    schemas = response.json()["components"]["schemas"]

    assert schemas["MessageRouteRequest"]["examples"] == [
        {
            "email": "jan.nowak@example.com",
            "message": "Nie działa mi komputer",
        }
    ]
    assert schemas["MessageRouteResponse"]["examples"] == [
        {
            "status": "sent",
            "recipient": "help-desk@example.com",
            "reply_to": "jan.nowak@example.com",
            "subject": "Zgłoszenie skierowane do: help-desk@example.com",
            "reason": "Wiadomość dotyczy problemu ze sprzętem komputerowym.",
            "used_fallback": False,
            "model": "qwen2.5:3b",
            "routing_confidence": "high",
        }
    ]


def test_openapi_message_route_operation_is_documented() -> None:
    response = request("GET", "/api/v1/openapi.json")
    operation = response.json()["paths"]["/api/v1/messages/route"]["post"]

    assert operation["summary"] == "Route a message to an internal department"
    assert "Ollama" in operation["description"]
    assert "send_email" in operation["description"]
    assert "Reply-To" in operation["description"]
    assert operation["responses"]["422"]["description"] == "Validation error."
    assert operation["responses"]["502"]["description"] == (
        "Upstream failure: the Ollama chat request failed, or the email "
        "could not be delivered via SMTP."
    )
    assert operation["responses"]["503"]["description"] == "Ollama is unavailable."
    # Every handled error status documents the ErrorResponse schema rather than an
    # undocumented free-form object (loop so dropping any one $ref is caught).
    for status in ("400", "413", "429", "502", "503"):
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/ErrorResponse"), status


def test_message_route_exists_under_api_v1() -> None:
    response = request_with_service(
        FakeMessageRoutingService(),
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"


def test_message_route_rejects_invalid_email() -> None:
    response = request(
        "POST",
        "/api/v1/messages/route",
        json={"email": "not-an-email", "message": "Hello"},
    )

    assert response.status_code == 422


def test_message_route_rejects_internationalized_email() -> None:
    # EAI addresses (Unicode in local-part or domain) are accepted by EmailStr
    # but would produce a malformed Reply-To header, so the API rejects them.
    for email in ("jań@example.com", "jan@münchen.example"):
        response = request(
            "POST",
            "/api/v1/messages/route",
            json={"email": email, "message": "Hello"},
        )

        assert response.status_code == 422, email


def test_message_route_accepts_sender_from_external_domain() -> None:
    # The sender address is self-reported and intentionally not restricted to a
    # fixed domain (see README, "Bezpieczeństwo i wyłapane pułapki"). A sender from
    # an external domain must pass validation and reach the routing service
    # unchanged; only the recipient is constrained to the allowed departments.
    service = FakeMessageRoutingService()

    response = request_with_service(
        service,
        "POST",
        "/api/v1/messages/route",
        json={"email": "outsider@gmail.com", "message": "Hello"},
    )

    assert response.status_code == 200
    assert service.requests[0].email == "outsider@gmail.com"


def test_message_route_rejects_missing_message() -> None:
    response = request(
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com"},
    )

    assert response.status_code == 422


def test_message_route_rejects_empty_message() -> None:
    response = request(
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": ""},
    )

    assert response.status_code == 422


def test_message_route_rejects_whitespace_only_message() -> None:
    response = request(
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "   "},
    )

    assert response.status_code == 422


def test_message_route_rejects_message_over_max_length() -> None:
    response = request(
        "POST",
        "/api/v1/messages/route",
        json={
            "email": "sender@example.com",
            "message": "x" * (MESSAGE_MAX_LENGTH + 1),
        },
    )

    assert response.status_code == 422


def test_message_route_valid_payload_returns_expected_response_fields() -> None:
    response = request_with_service(
        FakeMessageRoutingService(),
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "sent",
        "recipient": "help-desk@example.com",
        "reply_to": "sender@example.com",
        "subject": "Message routed to help-desk",
        "reason": "The message concerns a computer issue.",
        "used_fallback": False,
        "model": "qwen2.5:3b",
        "routing_confidence": "high",
    }


def test_message_route_delegates_to_message_routing_service() -> None:
    service = FakeMessageRoutingService()

    response = request_with_service(
        service,
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 200
    assert len(service.requests) == 1
    assert service.requests[0].email == "sender@example.com"
    assert service.requests[0].message == "Hello"


def test_message_route_maps_ollama_unavailable_to_503() -> None:
    response = request_with_service(
        RaisingMessageRoutingService(OllamaUnavailableError("Ollama unavailable")),
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Ollama is unavailable."


def test_message_route_maps_ollama_chat_error_to_502() -> None:
    response = request_with_service(
        RaisingMessageRoutingService(OllamaChatError("Ollama chat failed")),
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Ollama chat request failed."


def test_message_route_maps_email_delivery_error_to_502() -> None:
    response = request_with_service(
        RaisingMessageRoutingService(smtplib.SMTPException("SMTP failed")),
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Email delivery failed."


def test_message_route_maps_expected_value_error_to_400() -> None:
    response = request_with_service(
        RaisingMessageRoutingService(ValueError("Invalid tool arguments")),
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "The request could not be processed."
    # The raw exception message must never leak to the client.
    assert "Invalid tool arguments" not in response.text


class FakeReadinessProbe:
    def __init__(self, ready: bool) -> None:
        self._ready = ready
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def is_ready(self, *args: object, **kwargs: object) -> bool:
        self.calls.append((args, kwargs))
        return self._ready


class FakeOllamaReadinessProbe:
    def __init__(self, *, api_reachable: bool, model_available: bool) -> None:
        self._readiness = OllamaReadiness(
            api_reachable=api_reachable, model_available=model_available
        )
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def check_readiness(self, *args: object, **kwargs: object) -> OllamaReadiness:
        self.calls.append((args, kwargs))
        return self._readiness


def request_ready(*, ollama_api: bool, ollama_model: bool, smtp_ready: bool) -> httpx.Response:
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllamaReadinessProbe(
        api_reachable=ollama_api, model_available=ollama_model
    )
    app.dependency_overrides[get_mail_sender] = lambda: FakeReadinessProbe(smtp_ready)
    try:
        return request("GET", "/ready")
    finally:
        app.dependency_overrides.clear()


def test_ready_returns_200_when_all_checks_pass() -> None:
    response = request_ready(ollama_api=True, ollama_model=True, smtp_ready=True)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"ollama_api": True, "ollama_model": True, "smtp": True},
    }


def test_ready_returns_503_when_smtp_unreachable() -> None:
    response = request_ready(ollama_api=True, ollama_model=True, smtp_ready=False)

    assert response.status_code == 503
    assert response.json() == {
        "status": "not ready",
        "checks": {"ollama_api": True, "ollama_model": True, "smtp": False},
    }


def test_ready_returns_503_when_model_missing_even_if_api_reachable() -> None:
    # Ollama can be reachable while the configured model is absent; that is not ready.
    response = request_ready(ollama_api=True, ollama_model=False, smtp_ready=True)

    assert response.status_code == 503
    assert response.json() == {
        "status": "not ready",
        "checks": {"ollama_api": True, "ollama_model": False, "smtp": True},
    }


def test_ready_runs_probes_with_the_short_probe_timeout() -> None:
    # /ready must call each probe with the short readiness timeout (not the long
    # SMTP/request timeout). Guards the async change: reverting to the default
    # timeout, passing the wrong constant, or dropping the arg would fail here.
    from app.api.routes import _READY_PROBE_TIMEOUT_SECONDS

    ollama = FakeOllamaReadinessProbe(api_reachable=True, model_available=True)
    smtp = FakeReadinessProbe(ready=True)
    app.dependency_overrides[get_ollama_client] = lambda: ollama
    app.dependency_overrides[get_mail_sender] = lambda: smtp
    try:
        response = request("GET", "/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert ollama.calls and ollama.calls[0][0][0] == _READY_PROBE_TIMEOUT_SECONDS
    assert smtp.calls and smtp.calls[0][0][0] == _READY_PROBE_TIMEOUT_SECONDS


def test_response_includes_generated_request_id_header() -> None:
    response = request("GET", "/health")

    assert response.status_code == 200
    assert response.headers.get("x-request-id")


def test_request_id_header_is_echoed_when_provided() -> None:
    response = request("GET", "/health", headers={"X-Request-ID": "test-correlation-123"})

    assert response.headers.get("x-request-id") == "test-correlation-123"


def test_request_id_header_rejects_unsafe_value_and_generates_fresh_id() -> None:
    unsafe = "x" * 200 + " /;weird"
    response = request("GET", "/health", headers={"X-Request-ID": unsafe})

    echoed = response.headers.get("x-request-id")
    assert echoed is not None
    assert echoed != unsafe
    # An unsafe id is replaced by a fresh uuid4().hex (32 lowercase hex chars).
    assert len(echoed) == 32


def test_request_id_header_rejects_short_disallowed_charset() -> None:
    # Short value (well under the 128-char cap) but with characters outside the safe
    # allowlist. Isolates the charset filter from the length cap: this test would fail
    # if the charset restriction were dropped, even though the length check stays.
    unsafe = "bad;id/with spaces"
    response = request("GET", "/health", headers={"X-Request-ID": unsafe})

    echoed = response.headers.get("x-request-id")
    assert echoed is not None
    assert echoed != unsafe
    assert len(echoed) == 32  # fresh uuid4().hex
    assert echoed.isalnum()  # none of the disallowed chars survived


def test_request_id_reaches_sync_endpoint_log_records() -> None:
    # End-to-end: a known X-Request-ID must appear in a log record emitted by the
    # SYNC route_message handler, which FastAPI runs in the anyio threadpool. This is
    # why RequestIdMiddleware is pure-ASGI (the ContextVar is copied into
    # the worker thread); a revert to BaseHTTPMiddleware would silently break it.
    import io
    import logging as stdlib_logging

    from app.observability import JsonLogFormatter

    stream = io.StringIO()
    handler = stdlib_logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    routes_logger = stdlib_logging.getLogger("app.api.routes")
    routes_logger.addHandler(handler)
    previous_level = routes_logger.level
    routes_logger.setLevel(stdlib_logging.WARNING)
    try:
        response = request_with_service(
            RaisingMessageRoutingService(ValueError("boom")),
            "POST",
            "/api/v1/messages/route",
            headers={"X-Request-ID": "known-sync-id-123"},
            json={"email": "sender@example.com", "message": "Hello"},
        )
    finally:
        routes_logger.removeHandler(handler)
        routes_logger.setLevel(previous_level)

    assert response.status_code == 400
    logged = stream.getvalue()
    assert '"request_id": "known-sync-id-123"' in logged
    assert "Rejected message route request" in logged


def test_route_rejects_oversized_body_before_validation() -> None:
    # Body far over MAX_REQUEST_BODY_BYTES -> 413 from the ASGI guard, BEFORE the
    # Pydantic max_length 422, proving rejection happens before buffering/validation.
    huge = "x" * 200000
    response = request(
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": huge},
    )

    assert response.status_code == 413
    # The 413 is produced inside RequestIdMiddleware, so it still carries a
    # correlation id, exactly the abusive traffic an operator wants to trace.
    assert response.headers.get("x-request-id")


def test_route_rejects_oversized_chunked_body_without_content_length() -> None:
    # A chunked / streaming body has no Content-Length, so the fast-path check cannot
    # see it; the streaming guard must count actual bytes and still return 413.
    # Without it, a chunked body would bypass the limit and be buffered in full.
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(40):
            yield b"x" * 4096  # ~160 KB total, far over MAX_REQUEST_BODY_BYTES

    response = request("POST", "/api/v1/messages/route", content=chunks())

    assert response.status_code == 413
    # The streaming-guard 413 also flows out through RequestIdMiddleware (outermost).
    assert response.headers.get("x-request-id")


def test_route_returns_429_when_at_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from app.api import routes as routes_module

    busy = threading.BoundedSemaphore(1)
    busy.acquire()  # exhaust the only permit so the route's acquire fails
    monkeypatch.setattr(routes_module, "_route_semaphore", busy)

    response = request_with_service(
        FakeMessageRoutingService(),
        "POST",
        "/api/v1/messages/route",
        json={"email": "sender@example.com", "message": "Hello"},
    )

    assert response.status_code == 429
