import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

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


def read_script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_e2e_mail_check_imports_and_reuses_domain_recipients() -> None:
    module = load_module()

    assert callable(module.main)
    assert callable(module.addresses)
    assert module.ALLOWED_RECIPIENTS == ALLOWED_RECIPIENTS


def test_e2e_mail_check_uses_api_and_mailpit_endpoints() -> None:
    script = read_script()

    assert "/api/v1/messages/route" in script
    assert "/api/v1/messages?limit=50" in script
    assert "/api/v1/message/{message_id}" in script


def test_e2e_mail_check_validates_delivery_contract() -> None:
    script = read_script()

    assert "from app.domain.departments import ALLOWED_RECIPIENTS" in script
    assert 'status") != "sent"' in script
    assert "api_recipient not in ALLOWED_RECIPIENTS" in script
    assert "ReplyTo" in script
    assert "to_addresses[0] != api_recipient" in script


def test_e2e_mail_check_asserts_routing_correctness() -> None:
    script = read_script()

    # The unambiguous case routes to a specific department, checked on both the API
    # response and the captured Mailpit To, not merely "some allowed recipient".
    assert "EXPECTED_RECIPIENT" in script
    assert "Nie działa mi komputer" in script
    assert "help-desk@example.com" in script
    assert "api_recipient != EXPECTED_RECIPIENT" in script
    assert "to_addresses[0] != EXPECTED_RECIPIENT" in script


def test_e2e_mail_check_waits_for_ready_before_routing() -> None:
    module = load_module()
    script = read_script()

    assert callable(module.wait_for_ready)
    assert callable(module.probe_ready)
    assert "/ready" in script
    assert 'status") == "ready"' in script
    # The /ready wait must run before the route request is sent.
    assert script.index("wait_for_ready()") < script.index("{API_BASE_URL}{ROUTE_PATH}")


def test_e2e_mail_check_expected_recipient_default_is_allowed() -> None:
    module = load_module()

    assert module.EXPECTED_RECIPIENT in ALLOWED_RECIPIENTS


def test_probe_ready_handles_closed_port_without_raising() -> None:
    module = load_module()

    # A closed port must read as "not ready" (keep waiting), never raise.
    assert module.probe_ready("http://127.0.0.1:9/ready") is False


def test_e2e_mail_check_timeout_is_configurable_and_not_hardcoded() -> None:
    script = read_script()

    assert "REQUEST_TIMEOUT_SECONDS" in script
    assert "timeout=REQUEST_TIMEOUT_SECONDS" in script
    assert "timeout=30" not in script
    # Network failures (refused/timeout/reset) are handled, not surfaced raw.
    assert "except OSError" in script


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
