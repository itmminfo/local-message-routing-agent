import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from app.domain.departments import ALLOWED_RECIPIENTS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "routing_accuracy_check.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("routing_accuracy_check", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_routing_accuracy_cases_cover_all_departments() -> None:
    module = load_module()

    expected = {recipient for _message, recipient in module.CASES}
    assert expected == ALLOWED_RECIPIENTS


def test_routing_accuracy_uses_route_endpoint_and_domain_recipients() -> None:
    script = read_script()

    assert "/api/v1/messages/route" in script
    assert "from app.domain.departments import ALLOWED_RECIPIENTS" in script


def test_routing_accuracy_reports_hits_and_fallbacks() -> None:
    script = read_script()

    assert "Hits:" in script
    assert "Fallbacks:" in script
    assert "used_fallback" in script
    assert "HIT" in script
    assert "FALLBACK" in script
    assert "MISS" in script


def test_routing_accuracy_timeout_is_configurable_and_handles_network_errors() -> None:
    script = read_script()

    assert "REQUEST_TIMEOUT_SECONDS" in script
    assert "timeout=REQUEST_TIMEOUT_SECONDS" in script
    assert "except OSError" in script


def test_routing_accuracy_reports_unreachable_api_without_traceback() -> None:
    # Point at a closed port: expect a clean ERROR line and exit 1, no traceback.
    env = {
        **os.environ,
        "API_BASE_URL": "http://127.0.0.1:9",
        "REQUEST_TIMEOUT_SECONDS": "2",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert "Traceback" not in result.stderr


def test_require_http_url_rejects_non_http_scheme() -> None:
    module = load_module()

    with pytest.raises(SystemExit):
        module.require_http_url("file:///etc/passwd")


def test_require_http_url_accepts_http_and_https() -> None:
    module = load_module()

    module.require_http_url("http://localhost:8000")
    module.require_http_url("https://example.com")


def test_run_clear_cut_cases_counts_all_hits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_module()
    expected_by_message = dict(module.CASES)

    def fake_route_message(message: str) -> dict[str, object]:
        return {"recipient": expected_by_message[message], "used_fallback": False}

    monkeypatch.setattr(module, "route_message", fake_route_message)
    module.run_clear_cut_cases()

    out = capsys.readouterr().out
    total = len(module.CASES)
    assert f"Hits: {total}/{total}" in out


def test_run_boundary_cases_resolves_within_acceptable_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_module()
    acceptable_by_message = dict(module.BOUNDARY_CASES)

    def fake_route_message(message: str) -> dict[str, object]:
        return {"recipient": acceptable_by_message[message][0], "used_fallback": False}

    monkeypatch.setattr(module, "route_message", fake_route_message)
    module.run_boundary_cases()

    out = capsys.readouterr().out
    total = len(module.BOUNDARY_CASES)
    assert f"resolved within acceptable set: {total}/{total}" in out


def test_route_posts_payload_to_route_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"recipient": "it@example.com"}'

    def fake_urlopen(request: urllib.request.Request, timeout: object = None) -> "_FakeResponse":
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["data"] = request.data
        return _FakeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    result = module.route("Nie dziala komputer")

    assert result == {"recipient": "it@example.com"}
    assert str(captured["url"]).endswith("/api/v1/messages/route")
    assert captured["method"] == "POST"
    assert json.loads(captured["data"]) == {
        "email": module.SENDER_EMAIL,
        "message": "Nie dziala komputer",
    }


def test_held_out_cases_cover_all_departments() -> None:
    module = load_module()

    expected = {recipient for _message, recipient in module.HELD_OUT_CASES}
    assert expected == ALLOWED_RECIPIENTS


def test_held_out_cases_do_not_overlap_prompt() -> None:
    # Keep held-out cases separate from prompt examples, even after folding Polish
    # diacritics.
    import unicodedata

    from app.domain.routing_policy import ROUTING_EXAMPLES

    def fold(text: str) -> str:
        return (
            unicodedata.normalize("NFKD", text)
            .encode("ascii", "ignore")
            .decode("ascii")
            .lower()
            .strip()
        )

    module = load_module()
    prompt = {fold(message) for message, _ in ROUTING_EXAMPLES}
    held_out = {fold(message) for message, _ in module.HELD_OUT_CASES}
    assert held_out.isdisjoint(prompt)


def test_run_held_out_cases_counts_all_hits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_module()
    expected_by_message = dict(module.HELD_OUT_CASES)

    def fake_route_message(message: str) -> dict[str, object]:
        return {"recipient": expected_by_message[message], "used_fallback": False}

    monkeypatch.setattr(module, "route_message", fake_route_message)
    module.run_held_out_cases()

    out = capsys.readouterr().out
    total = len(module.HELD_OUT_CASES)
    assert f"Hits: {total}/{total}" in out


def test_run_held_out_cases_reports_misses_and_fallbacks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Guards against the verdict/counter logic being a no-op: feed one wrong-but-not-
    # fallback result (MISS) and one wrong-via-fallback result (FALLBACK) and assert
    # both verdict branches and both counters react.
    module = load_module()
    expected_by_message = dict(module.HELD_OUT_CASES)
    miss_message = module.HELD_OUT_CASES[0][0]
    fallback_message = module.HELD_OUT_CASES[1][0]

    def wrong_recipient(message: str) -> str:
        expected = expected_by_message[message]
        return "it@example.com" if expected != "it@example.com" else "kadry@example.com"

    def fake_route_message(message: str) -> dict[str, object]:
        if message == miss_message:
            return {"recipient": wrong_recipient(message), "used_fallback": False}
        if message == fallback_message:
            return {"recipient": wrong_recipient(message), "used_fallback": True}
        return {"recipient": expected_by_message[message], "used_fallback": False}

    monkeypatch.setattr(module, "route_message", fake_route_message)
    module.run_held_out_cases()

    out = capsys.readouterr().out
    total = len(module.HELD_OUT_CASES)
    assert "[MISS" in out
    assert "[FALLBACK" in out
    assert f"Hits: {total - 2}/{total}" in out
    assert f"Fallbacks: 1/{total}" in out
