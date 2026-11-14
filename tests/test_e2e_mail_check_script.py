import importlib.util
import os
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from app.domain.departments import ALLOWED_RECIPIENTS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "e2e_mail_check.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_mail_check", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_e2e_mail_check_imports_and_reuses_domain_recipients() -> None:
    module = load_module()

    assert callable(module.main)
    assert callable(module.addresses)
    assert module.ALLOWED_RECIPIENTS == ALLOWED_RECIPIENTS


def install_fake_stack(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    route_changes: dict[str, Any] | None = None,
    mail_changes: dict[str, Any] | None = None,
) -> list[str]:
    calls: list[str] = []
    mail_reads = 0
    record: dict[str, Any] = {
        "id": "preview-id",
        "version": 1,
        "payload_digest": "a" * 64,
        "state": "pending_review",
        "attempts": [],
        "recipient": None,
        "suggestion": {"status": "not_requested"},
        "message_id": "<stable-id@example.test>",
    }
    sent = {
        **record,
        "state": "sent",
        "recipient": module.EXPECTED_RECIPIENT,
        "attempts": [{"id": "attempt-id"}],
    }
    if route_changes:
        sent.update(route_changes)
    mail_detail = {
        "To": [{"Address": module.EXPECTED_RECIPIENT}],
        "ReplyTo": [{"Address": module.SENDER_EMAIL}],
        "MessageID": "stable-id@example.test",
        "Text": (
            f"Dział zatwierdzony przez operatora: {module.EXPECTED_RECIPIENT}\n\n"
            f"Treść zgłoszenia:\n{module.ROUTE_MESSAGE}\n\n"
        ),
    }
    if mail_changes:
        mail_detail.update(mail_changes)

    class FakeReviewClient:
        def __init__(self, base_url: str, *, timeout: int) -> None:
            assert base_url == module.API_BASE_URL
            assert timeout == module.REQUEST_TIMEOUT_SECONDS

        def start_session(self) -> dict[str, Any]:
            calls.append("session")
            return {"csrf_token": "test-csrf"}

        def request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(f"{method} {path}")
            if path == module.REVIEW_PATH:
                assert payload == {"email": module.SENDER_EMAIL, "message": module.ROUTE_MESSAGE}
                return record.copy()
            if path.endswith("/suggest"):
                assert payload == {"version": 1}
                return {**record, "suggestion": {"status": "ready"}}
            assert path == module.REVIEW_PATH + "/preview-id/approve"
            assert payload == {
                "version": 1,
                "payload_digest": "a" * 64,
                "recipient": module.EXPECTED_RECIPIENT,
                "confirmed": True,
            }
            return sent.copy()

    def fake_request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
        nonlocal mail_reads
        calls.append(f"{method} {url}")
        if method == "GET" and url == f"{module.MAIL_API_URL}{module.MESSAGES_PATH}":
            mail_reads += 1
            return {"messages": [] if mail_reads <= 2 else [{"ID": "new-message"}]}
        if method == "GET" and url == f"{module.MAIL_API_URL}/api/v1/message/new-message":
            return mail_detail
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(module, "wait_for_ready", lambda: calls.append("ready"))
    monkeypatch.setattr(module, "ReviewClient", FakeReviewClient)
    monkeypatch.setattr(module, "request_json", fake_request_json)
    return calls


def test_e2e_main_waits_for_ready_and_checks_api_and_delivered_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    calls = install_fake_stack(module, monkeypatch)

    assert module.main() == 0
    assert calls == [
        "ready",
        f"GET {module.MAIL_API_URL}{module.MESSAGES_PATH}",
        "session",
        f"POST {module.REVIEW_PATH}",
        f"GET {module.MAIL_API_URL}{module.MESSAGES_PATH}",
        f"POST {module.REVIEW_PATH}/preview-id/approve",
        f"POST {module.REVIEW_PATH}/preview-id/approve",
        f"GET {module.MAIL_API_URL}{module.MESSAGES_PATH}",
        f"GET {module.MAIL_API_URL}/api/v1/message/new-message",
    ]


@pytest.mark.parametrize(
    ("mail_changes", "error"),
    [
        ({"Text": ""}, "non-empty text body"),
        ({"Text": "Treść zgłoszenia:\ninna wiadomość\n\n"}, "original message"),
        (
            {"Text": "Treść zgłoszenia:\nNie działa mi komputer\n\nUzasadnienie (AI): błędne"},
            "operator-approved department",
        ),
        ({"ReplyTo": [{"Address": "wrong@example.com"}]}, "Reply-To is incorrect"),
        ({"To": [{"Address": "it@example.com"}]}, "Mailpit To does not match"),
    ],
)
def test_e2e_main_rejects_corrupt_mailpit_delivery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mail_changes: dict[str, Any],
    error: str,
) -> None:
    module = load_module()
    install_fake_stack(module, monkeypatch, mail_changes=mail_changes)

    with pytest.raises(SystemExit):
        module.main()

    assert error in capsys.readouterr().err


def test_e2e_main_rejects_wrong_allowed_api_recipient(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_module()
    install_fake_stack(module, monkeypatch, route_changes={"recipient": "it@example.com"})

    with pytest.raises(SystemExit):
        module.main()

    assert "expected 'help-desk@example.com'" in capsys.readouterr().err


def test_e2e_main_can_require_suggestion_before_manual_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setattr(module, "REQUIRE_SUGGESTION", True)
    calls = install_fake_stack(module, monkeypatch)
    assert module.main() == 0
    assert calls.index(f"POST {module.REVIEW_PATH}/preview-id/suggest") < calls.index(
        f"POST {module.REVIEW_PATH}/preview-id/approve"
    )


@pytest.mark.parametrize(
    ("route_message", "reason"),
    [
        ("Nie działa mi komputer\r\nDrukarka nie drukuje", "Problem ze sprzętem."),
        ("Nie działa mi komputer", "Problem ze sprzętem.\r\nWymaga pomocy."),
    ],
)
def test_e2e_main_accepts_mailpit_newline_normalization(
    monkeypatch: pytest.MonkeyPatch, route_message: str, reason: str
) -> None:
    module = load_module()
    monkeypatch.setattr(module, "ROUTE_MESSAGE", route_message)
    install_fake_stack(module, monkeypatch, route_changes={"reason": reason})

    assert module.main() == 0


def test_e2e_mail_check_expected_recipient_default_is_allowed() -> None:
    module = load_module()

    assert module.EXPECTED_RECIPIENT in ALLOWED_RECIPIENTS


def test_probe_ready_handles_closed_port_without_raising() -> None:
    module = load_module()

    # A closed port must read as "not ready" (keep waiting), never raise.
    assert module.probe_ready("http://127.0.0.1:9/ready") is False


def test_e2e_mail_check_timeout_is_configurable_and_not_hardcoded() -> None:
    module = load_module()
    seen: list[int] = []

    def fake_urlopen(_request: Any, *, timeout: int) -> Any:
        seen.append(timeout)
        raise urllib.error.URLError("connection refused")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(module.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(SystemExit):
            module.request_json("GET", "http://localhost:8000/health")

    assert seen == [module.REQUEST_TIMEOUT_SECONDS]


def test_e2e_mail_check_reports_unreachable_api_without_traceback() -> None:
    # Point at a closed port: expect a clean ERROR line and exit 1, no traceback.
    # READY_TIMEOUT_SECONDS is kept tiny so the up-front /ready wait fails fast here
    # instead of polling the closed port for the full default window.
    env = {
        **os.environ,
        "API_BASE_URL": "http://127.0.0.1:9",
        "MAIL_API_URL": "http://127.0.0.1:9",
        "REQUEST_TIMEOUT_SECONDS": "2",
        "READY_TIMEOUT_SECONDS": "1",
        "READY_PROBE_TIMEOUT_SECONDS": "1",
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


def test_addresses_extracts_from_dicts_and_strings() -> None:
    module = load_module()

    out = module.addresses([{"Address": "a@b.com"}, {"address": "c@d.com"}, "e@f.com", {"x": 1}, 7])

    assert out == ["a@b.com", "c@d.com", "e@f.com"]


def test_addresses_returns_empty_for_non_list() -> None:
    module = load_module()

    assert module.addresses("nope") == []
    assert module.addresses(None) == []


def test_message_id_reads_id_or_lowercase_id() -> None:
    module = load_module()

    assert module.message_id({"ID": "abc"}) == "abc"
    assert module.message_id({"id": "xyz"}) == "xyz"


def test_message_id_fails_when_missing() -> None:
    module = load_module()

    with pytest.raises(SystemExit):
        module.message_id({})


def test_messages_from_response_supports_both_keys_and_filters_non_dicts() -> None:
    module = load_module()

    result = module.messages_from_response({"messages": [{"ID": "1"}, "junk", 2]})
    assert result == [{"ID": "1"}]
    assert module.messages_from_response({"Messages": [{"ID": "9"}]}) == [{"ID": "9"}]


def test_messages_from_response_fails_without_list() -> None:
    module = load_module()

    with pytest.raises(SystemExit):
        module.messages_from_response({"foo": 1})
