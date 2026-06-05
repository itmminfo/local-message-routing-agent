#!/usr/bin/env python3
"""End-to-end delivery + routing-correctness check against a running stack.

First waits for the API's /ready probe (Ollama API + model present + SMTP reachable)
so a cold or still-starting stack does not race the request. Then sends one route
request, reads Mailpit through its HTTP API, and verifies two distinct things:

1. Delivery contract (always): a new message appeared, Reply-To matches the sender
   email, To is exactly one allowed department recipient, and the captured Mailpit
   recipient matches the API response recipient.
2. Routing correctness (one unambiguous case): the message routes to the EXPECTED
   recipient, in both the API response and the captured Mailpit To header. This
   catches a regression that misroutes (e.g. to it@ or the other@ fallback) instead
   of passing as long as *some* allowed recipient was used. ROUTE_MESSAGE /
   EXPECTED_RECIPIENT default to an in-prompt, single-intent case so a real model at
   temperature 0 is effectively deterministic here; both are overridable by env.

Assumes the Docker Compose environment is already running.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.departments import ALLOWED_RECIPIENTS  # noqa: E402

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

ROUTE_PATH = "/api/v1/messages/route"
READY_PATH = "/ready"
MESSAGES_PATH = "/api/v1/messages?limit=50"
MESSAGE_DETAIL_PATH = "/api/v1/message/{message_id}"


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


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
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
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
    return isinstance(parsed, dict) and parsed.get("status") == "ready"


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

    route_response = request_json(
        "POST",
        f"{API_BASE_URL}{ROUTE_PATH}",
        {"email": SENDER_EMAIL, "message": ROUTE_MESSAGE},
    )

    if route_response.get("status") != "sent":
        fail(f"API does not return status sent: {route_response!r}")

    api_recipient = route_response.get("recipient")
    if not isinstance(api_recipient, str):
        fail(f"API response recipient is missing or not a string: {route_response!r}")

    if api_recipient not in ALLOWED_RECIPIENTS:
        fail(f"API recipient is outside allowed recipients: {api_recipient!r}")

    # Routing correctness (distinct from the delivery contract above): an unambiguous
    # message must reach a specific department, so misrouting to the wrong department
    # or the other@ fallback fails here instead of passing as "some allowed recipient".
    if api_recipient != EXPECTED_RECIPIENT:
        fail(f"API routed {ROUTE_MESSAGE!r} to {api_recipient!r}, expected {EXPECTED_RECIPIENT!r}.")

    new_summary = None
    deadline = time.time() + MAIL_POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        current = request_json("GET", f"{MAIL_API_URL}{MESSAGES_PATH}")
        new_messages = [
            message
            for message in messages_from_response(current)
            if message_id(message) not in baseline_ids
        ]
        if new_messages:
            new_summary = new_messages[0]
            break
        time.sleep(1)

    if new_summary is None:
        fail("No new message appeared in Mailpit after the API request.")

    encoded_id = urllib.parse.quote(message_id(new_summary), safe="")
    detail_url = f"{MAIL_API_URL}{MESSAGE_DETAIL_PATH.format(message_id=encoded_id)}"
    detail = request_json("GET", detail_url)

    to_addresses = addresses(detail.get("To"))
    reply_to_addresses = addresses(detail.get("ReplyTo"))

    if len(reply_to_addresses) != 1 or reply_to_addresses[0] != SENDER_EMAIL:
        fail(f"Reply-To is incorrect: {reply_to_addresses!r}")

    if len(to_addresses) != 1:
        fail(f"Expected exactly one Mailpit To address, got: {to_addresses!r}")

    if to_addresses[0] not in ALLOWED_RECIPIENTS:
        fail(f"Mailpit To is outside allowed recipients: {to_addresses!r}")

    if to_addresses[0] != api_recipient:
        fail(
            "Mailpit To does not match API recipient: "
            f"API recipient={api_recipient!r}, Mailpit recipient={to_addresses[0]!r}"
        )

    # The captured To must also be the expected department: closes the routing-
    # correctness check on the mail itself, not only on the API response.
    if to_addresses[0] != EXPECTED_RECIPIENT:
        fail(
            f"Mailpit To {to_addresses[0]!r} does not match "
            f"expected recipient {EXPECTED_RECIPIENT!r}."
        )

    print(f"Message: {ROUTE_MESSAGE}")
    print(f"Expected recipient: {EXPECTED_RECIPIENT}")
    print(f"API recipient: {api_recipient}")
    print(f"Mailpit recipient: {to_addresses[0]}")
    print(f"Reply-To: {reply_to_addresses[0]}")
    print("Routing correctness: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
