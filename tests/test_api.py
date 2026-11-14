import asyncio
from typing import Any

import httpx
import pytest
from app.api.routes import (
    get_mail_sender,
    get_ollama_client,
)
from app.config import get_settings
from app.infrastructure.ollama_client import (
    OllamaReadiness,
)
from app.main import app, create_app
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
            base_url="http://localhost",
        ) as client:
            headers = {"Origin": "http://localhost", **kwargs.pop("headers", {})}
            return await client.request(method, url, headers=headers, **kwargs)

    return asyncio.run(call_app())


def request(method: str, url: str, **kwargs: object) -> httpx.Response:
    return request_app(app, method, url, **kwargs)


def test_api_app_imports() -> None:
    assert app.title == "Local Message Routing Agent"
    assert app.docs_url == "/api/v1/docs"
    assert app.redoc_url == "/api/v1/redoc"
    assert app.openapi_url == "/api/v1/openapi.json"


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


def test_local_eval_route_is_hidden_from_openapi() -> None:
    schema = request("GET", "/api/v1/openapi.json").json()
    assert "/api/v1/messages/route-local-eval" not in schema["paths"]


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


def test_create_app_uses_configured_app_name_and_api_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_NAME", "Configured App")
    monkeypatch.setenv("API_PREFIX", "/custom")
    get_settings.cache_clear()
    target = create_app()
    assert target.title == "Configured App"
    assert target.docs_url == "/custom/docs"
    assert target.redoc_url == "/custom/redoc"
    assert target.openapi_url == "/custom/openapi.json"
    assert (
        request_app(
            target,
            "POST",
            "/custom/messages/route",
            json={"email": "person@example.com", "message": "Issue"},
        ).status_code
        == 409
    )


def test_legacy_operation_documents_required_review() -> None:
    operation = request("GET", "/api/v1/openapi.json").json()["paths"]["/api/v1/messages/route"][
        "post"
    ]
    assert "409" in operation["responses"]
    assert "200" not in operation["responses"]
    assert operation["deprecated"] is True
