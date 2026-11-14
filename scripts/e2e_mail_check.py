#!/usr/bin/env python3
"""Synthetic acceptance of local review and Mailpit delivery, never model accuracy.

Creates an immutable preview, proves no message arrived before approval, then
explicitly approves EXPECTED_RECIPIENT as a test operator. Replaying the approval
must not send another copy. This automated test does not prove human presence.
Set REQUIRE_SUGGESTION=true to also check optional model proposal plumbing.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.headerregistry import BaseHeader
from email.parser import HeaderParser
from email.policy import default
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.departments import ALLOWED_RECIPIENTS  # noqa: E402

from scripts.review_client import ReviewClient  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
MAIL_API_URL = os.environ.get("MAIL_API_URL", "http://localhost:8025").rstrip("/")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "e2e-mail-check@example.com")
# Unambiguous, in-prompt single-intent case (see app/domain/routing_policy.py), so a
# real model at temperature 0 is effectively deterministic and the CI mock resolves it
# by keyword. Override both to probe a different routing decision.
ROUTE_MESSAGE = os.environ.get("ROUTE_MESSAGE", "Nie działa mi komputer")
EXPECTED_RECIPIENT = os.environ.get("EXPECTED_RECIPIENT", "help-desk@example.com")
# Default is generous so a cold or CPU-only first inference does not time out.
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "120"))
MAIL_POLL_TIMEOUT_SECONDS = int(os.environ.get("MAIL_POLL_TIMEOUT_SECONDS", "30"))
# Wait for /ready before sending: how long to keep polling, and the per-probe timeout.
READY_TIMEOUT_SECONDS = int(os.environ.get("READY_TIMEOUT_SECONDS", "120"))
READY_PROBE_TIMEOUT_SECONDS = int(os.environ.get("READY_PROBE_TIMEOUT_SECONDS", "5"))

REVIEW_PATH = "/api/v1/review/previews"
READY_PATH = "/health"
REQUIRE_SUGGESTION = os.environ.get("REQUIRE_SUGGESTION", "false").lower() == "true"
MESSAGES_PATH = "/api/v1/messages?limit=50"
MESSAGE_DETAIL_PATH = "/api/v1/message/{message_id}"


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def normalize_newlines(value: str) -> str:
    # EmailMessage and Mailpit normalize body line endings independently of the
    # original API payload. Compare equivalent text instead of raw CRLF bytes.
    return value.replace("\r\n", "\n").replace("\r", "\n")


def require_message_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.startswith("<")
        or not value.endswith(">")
        or "\r" in value
        or "\n" in value
    ):
        fail("API did not return a valid RFC Message-ID.")
    header = HeaderParser(policy=default).parsestr(f"Message-ID: {value}\n\n")["Message-ID"]
    if not isinstance(header, BaseHeader) or header.defects:
        fail("API did not return a valid RFC Message-ID.")
    return value


_ALLOWED_URL_SCHEMES = ("http", "https")


def require_http_url(url: str) -> None:
    # The base URLs come from environment variables; restrict them to HTTP(S) so
    # a stray file:// or other scheme cannot be opened (Bandit B310).
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in _ALLOWED_URL_SCHEMES:
        fail(f"Refusing non-HTTP(S) URL scheme {scheme!r}: {url}")


def request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    require_http_url(url)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(  # nosec B310
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        fail(f"{method} {url} returned HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        fail(f"{method} {url} failed: {exc.reason}")
    except OSError as exc:
        # Catches read timeouts (TimeoutError), connection resets and other
        # socket errors that urllib does not wrap in URLError.
        fail(f"{method} {url} failed: {exc}")

    try:
        return json.loads(response_body)
    except json.JSONDecodeError as exc:
        fail(f"{method} {url} did not return JSON: {exc}")


def messages_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    messages = response.get("messages")
    if messages is None:
        messages = response.get("Messages")
    if not isinstance(messages, list):
        fail("Mail API response did not include a messages list.")
    return [message for message in messages if isinstance(message, dict)]


def message_id(message: dict[str, Any]) -> str:
    value = message.get("ID") or message.get("id")
    if not isinstance(value, str) or not value:
        fail(f"Mail API message did not include an ID: {message!r}")
    return value


def addresses(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        if isinstance(item, dict):
            address = item.get("Address") or item.get("address")
            if isinstance(address, str) and address:
                result.append(address)
        elif isinstance(item, str) and item:
            result.append(item)
    return result


def probe_ready(url: str) -> bool:
    # One readiness probe. A 503 (not ready yet) or a connection error (API still
    # starting) is a "keep waiting" signal, not a hard failure, so this never calls
    # fail(); only an unexpected scheme would. Returns True only on 200 + ready.
    require_http_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Host": urllib.parse.urlsplit(os.environ.get("REVIEW_ORIGIN", API_BASE_URL)).netloc,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # nosec B310
            request, timeout=READY_PROBE_TIMEOUT_SECONDS
        ) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        exc.read()
        return False
    except (urllib.error.URLError, OSError):
        return False

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("status") == "ok"


def wait_for_ready() -> None:
    url = f"{API_BASE_URL}{READY_PATH}"
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if probe_ready(url):
            return
        time.sleep(2)
    fail(f"API did not become ready at {url} within {READY_TIMEOUT_SECONDS}s.")


def main() -> int:
    if EXPECTED_RECIPIENT not in ALLOWED_RECIPIENTS:
        fail(f"EXPECTED_RECIPIENT is not an allowed recipient: {EXPECTED_RECIPIENT!r}")
    wait_for_ready()
    baseline = request_json("GET", f"{MAIL_API_URL}{MESSAGES_PATH}")
    baseline_ids = {message_id(message) for message in messages_from_response(baseline)}
    client = ReviewClient(API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        client.start_session()
        preview = client.request(
            "POST", REVIEW_PATH, {"email": SENDER_EMAIL, "message": ROUTE_MESSAGE}
        )
        if preview.get("state") != "pending_review" or preview.get("attempts") != []:
            fail("Creating a preview must return pending_review without delivery attempts.")
        path = f"{REVIEW_PATH}/{preview['id']}"
        if REQUIRE_SUGGESTION:
            preview = client.request("POST", f"{path}/suggest", {"version": preview["version"]})
            if preview.get("suggestion", {}).get("status") != "ready":
                fail("The optional proposal did not succeed.")
        before = request_json("GET", f"{MAIL_API_URL}{MESSAGES_PATH}")
        if {message_id(item) for item in messages_from_response(before)} != baseline_ids:
            fail("Mail appeared before explicit approval.")
        approval = {
            "version": preview["version"],
            "payload_digest": preview["payload_digest"],
            "recipient": EXPECTED_RECIPIENT,
            "confirmed": True,
        }
        sent = client.request("POST", f"{path}/approve", approval)
        replay = client.request("POST", f"{path}/approve", approval)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        fail(f"Review request failed: {exc}")
    if sent.get("state") != "sent" or replay.get("state") != "sent":
        fail(f"Approval did not confirm Mailpit delivery: {sent!r}, replay={replay!r}")
    if sent.get("recipient") != EXPECTED_RECIPIENT:
        fail(f"API recipient must equal expected {EXPECTED_RECIPIENT!r}: {sent.get('recipient')!r}")
    if len(sent.get("attempts", [])) != 1 or len(replay.get("attempts", [])) != 1:
        fail("Replay started another delivery attempt.")
    stable_message_id = require_message_id(sent.get("message_id"))
    if stable_message_id != replay.get("message_id"):
        fail("Replay changed the stable Message-ID.")
    new_messages = []
    deadline = time.time() + MAIL_POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        current = request_json("GET", f"{MAIL_API_URL}{MESSAGES_PATH}")
        new_messages = [
            item for item in messages_from_response(current) if message_id(item) not in baseline_ids
        ]
        if new_messages:
            break
        time.sleep(1)
    if len(new_messages) != 1:
        fail(f"Expected exactly one new message after approval/replay, got {len(new_messages)}.")
    encoded_id = urllib.parse.quote(message_id(new_messages[0]), safe="")
    detail = request_json(
        "GET", f"{MAIL_API_URL}{MESSAGE_DETAIL_PATH.format(message_id=encoded_id)}"
    )
    to_addresses = addresses(detail.get("To"))
    reply_to_addresses = addresses(detail.get("ReplyTo"))
    if to_addresses != [EXPECTED_RECIPIENT]:
        fail(f"Mailpit To does not match approved recipient: {to_addresses!r}")
    if reply_to_addresses != [SENDER_EMAIL]:
        fail(f"Reply-To is incorrect: {reply_to_addresses!r}")
    if detail.get("Subject") != f"Zgłoszenie skierowane do: {EXPECTED_RECIPIENT}":
        fail("Mailpit Subject does not match the approved recipient.")
    mail_text = detail.get("Text")
    if not isinstance(mail_text, str) or not mail_text.strip():
        fail("Mailpit did not capture a non-empty text body.")
    mail_text = normalize_newlines(mail_text)
    original = normalize_newlines(ROUTE_MESSAGE)
    if f"Treść zgłoszenia:\n{original}\n\n" not in mail_text:
        fail("Mailpit text body does not contain the original message in its template.")
    if f"Dział zatwierdzony przez operatora: {EXPECTED_RECIPIENT}" not in mail_text:
        fail("Mailpit text body does not include the operator-approved department.")
    expected_text = (
        "Wiadomość przekazana po lokalnym zatwierdzeniu.\n\n"
        f"Nadawca: {SENDER_EMAIL}\n"
        f"Dział zatwierdzony przez operatora: {EXPECTED_RECIPIENT}\n\n"
        f"Treść zgłoszenia:\n{original}\n\n"
    )
    if mail_text != expected_text:
        fail("Mailpit text body does not match the complete approved message template.")
    # Mailpit normally strips the RFC angle brackets; do not coerce missing IDs.
    if detail.get("MessageID") not in (stable_message_id, stable_message_id[1:-1]):
        fail("Mailpit Message-ID does not match the persisted attempt.")
    print(f"Preview: {preview['id']}")
    print(f"Approved recipient: {EXPECTED_RECIPIENT}")
    print(f"Message-ID: {sent['message_id']}")
    print("Before approval: no mail. After approval and replay: exactly one mail.")
    print("Mailpit To / Reply-To / immutable original / operator approval: OK")
    print("This is synthetic approval testing; model accuracy and human presence are not measured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
