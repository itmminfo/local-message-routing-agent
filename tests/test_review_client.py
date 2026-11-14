import json
import threading
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from scripts.review_client import ReviewClient


@dataclass
class CapturedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class LocalServer:
    url: str
    requests: list[CapturedRequest] = field(default_factory=list)
    responses: dict[tuple[str, str], tuple[int, dict[str, str], Any]] = field(default_factory=dict)


@contextmanager
def local_server() -> Iterator[LocalServer]:
    """A real HTTP boundary, bound only to the ephemeral loopback interface."""
    captured = LocalServer("")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            captured.requests.append(
                CapturedRequest(
                    self.command,
                    self.path,
                    {name.lower(): value for name, value in self.headers.items()},
                    self.rfile.read(size),
                )
            )
            status, headers, payload = captured.responses.get(
                (self.command, self.path), (200, {}, {"ok": True})
            )
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self.respond()

        def do_POST(self) -> None:
            self.respond()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    captured.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_client_bootstrap_keeps_cookie_and_uses_exact_host_origin_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVIEW_ORIGIN", raising=False)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    with local_server() as server:
        server.responses[("GET", "/api/v1/review/session")] = (
            200,
            {"Set-Cookie": "review_session=local-only; HttpOnly; SameSite=Strict; Path=/"},
            {"csrf_token": "session-csrf", "departments": []},
        )
        origin = "http://localhost:18000"
        client = ReviewClient(server.url, origin=origin)

        assert client.start_session()["csrf_token"] == "session-csrf"
        client.request("GET", "/api/v1/review/previews/known")
        payload = {"email": "operator@example.test", "message": "Treść zgłoszenia"}
        client.request("POST", "/api/v1/review/previews", payload)

        bootstrap, read, write = server.requests
        assert bootstrap.headers["host"] == "localhost:18000"
        assert "origin" not in bootstrap.headers
        assert "x-csrf-token" not in bootstrap.headers
        assert "cookie" not in bootstrap.headers
        assert read.headers["cookie"] == "review_session=local-only"
        assert "origin" not in read.headers
        assert "x-csrf-token" not in read.headers
        assert write.headers["host"] == "localhost:18000"
        assert write.headers["origin"] == origin
        assert write.headers["x-csrf-token"] == "session-csrf"
        assert write.headers["cookie"] == "review_session=local-only"
        assert write.headers["content-type"] == "application/json"
        assert json.loads(write.body) == payload


@pytest.mark.parametrize(
    ("base_url", "origin"),
    [
        ("https://127.0.0.1:8000", "http://localhost:8000"),
        ("http://external.invalid", "http://localhost:8000"),
        ("http://user:pass@localhost:8000", "http://localhost:8000"),
        ("http://localhost:8000/prefix", "http://localhost:8000"),
        ("http://localhost:8000?query=1", "http://localhost:8000"),
        ("http://localhost:8000#fragment", "http://localhost:8000"),
        ("file:///tmp/review", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "https://localhost:8000"),
        ("http://127.0.0.1:8000", "http://external.invalid"),
        ("http://127.0.0.1:8000", "http://api:8000"),
        ("http://127.0.0.1:8000", "http://user:pass@localhost:8000"),
        ("http://127.0.0.1:8000", "http://localhost:8000/path"),
        ("http://127.0.0.1:8000", "http://localhost:8000?query=1"),
        ("http://127.0.0.1:8000", "http://localhost:8000#fragment"),
    ],
)
def test_client_rejects_nonlocal_or_ambiguous_targets_before_network(
    base_url: str, origin: str
) -> None:
    with pytest.raises(ValueError, match="local HTTP API"):
        ReviewClient(base_url, origin)


@pytest.mark.parametrize(
    ("base_url", "origin"),
    [
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("http://api:8000", "http://127.0.0.1:18000"),
    ],
)
def test_client_accepts_loopback_and_compose_api_targets(base_url: str, origin: str) -> None:
    client = ReviewClient(base_url, origin)
    assert client.origin == origin


def test_client_reads_operator_origin_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_ORIGIN", "http://127.0.0.1:18000")
    client = ReviewClient("http://api:8000")
    assert client.host == "127.0.0.1:18000"
    assert client.origin == "http://127.0.0.1:18000"


@pytest.mark.parametrize(
    "path", ["https://external.invalid/leak", "//external.invalid/leak", "relative"]
)
def test_client_rejects_nonlocal_request_paths(path: str) -> None:
    with local_server() as server:
        client = ReviewClient(server.url, origin=server.url)
        with pytest.raises(ValueError, match="path must be local"):
            client.request("POST", path, {"confirmed": True})
        assert server.requests == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_does_not_forward_csrf_cookie_or_body(status: int) -> None:
    with local_server() as destination, local_server() as source:
        source.responses[("GET", "/api/v1/review/session")] = (
            200,
            {"Set-Cookie": "review_session=secret-session; Path=/"},
            {"csrf_token": "secret-token"},
        )
        source.responses[("POST", "/redirect")] = (
            status,
            {"Location": f"{destination.url}/leak"},
            {"detail": "redirect"},
        )
        client = ReviewClient(source.url, origin=source.url)
        client.start_session()
        with pytest.raises(urllib.error.HTTPError) as error:
            client.request("POST", "/redirect", {"message": "private payload"})

        assert error.value.code == status
        assert len(source.requests) == 2
        assert source.requests[1].headers["x-csrf-token"] == "secret-token"
        assert source.requests[1].headers["cookie"] == "review_session=secret-session"
        assert destination.requests == []


@pytest.mark.parametrize("token", [None, "", 123, []])
def test_bootstrap_rejects_missing_or_invalid_csrf_token(token: object) -> None:
    with local_server() as server:
        server.responses[("GET", "/api/v1/review/session")] = (
            200,
            {},
            {"csrf_token": token},
        )
        client = ReviewClient(server.url, origin=server.url)
        with pytest.raises(ValueError, match="CSRF token"):
            client.start_session()
        assert client.csrf_token == ""


def test_client_rejects_nonobject_response() -> None:
    with local_server() as server:
        server.responses[("GET", "/api/v1/review/session")] = (200, {}, ["unexpected"])
        client = ReviewClient(server.url, origin=server.url)
        with pytest.raises(ValueError, match="non-object"):
            client.start_session()
