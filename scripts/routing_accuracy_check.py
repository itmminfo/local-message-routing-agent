#!/usr/bin/env python3
"""Non-deterministic routing accuracy report.

Sends a set of labeled messages to the running API and prints, per case, where
the model routed the message. It is a diagnostic, not a pass/fail gate.

The report has three parts:

- "In-prompt cases" reuse messages that also appear as few-shot examples in the
  prompt, so a high score there mainly checks memorization, not generalization.
- "Held-out cases" do not appear in the prompt and are the real generalization
  signal.
- "Boundary cases" sit on the seams the task deliberately leaves undefined
  (human-resources vs kadry, help-desk vs IT). Each accepts a set of defensible
  recipients and is reported separately; it never counts as a failure, because
  the point is to observe how the model resolves an ambiguity that has no single
  correct answer.

Assumes the Docker Compose environment is already running.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.departments import ALLOWED_RECIPIENTS  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "routing-accuracy@example.com")
ROUTE_URL = f"{API_BASE_URL}/api/v1/messages/route"
# Default is generous so a cold or CPU-only inference does not time out.
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "180"))

_ALLOWED_URL_SCHEMES = ("http", "https")


def require_http_url(url: str) -> None:
    # API_BASE_URL comes from the environment; restrict it to HTTP(S) so a stray
    # file:// or other scheme cannot be opened (Bandit B310).
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise SystemExit(f"ERROR: refusing non-HTTP(S) URL scheme {scheme!r}: {url}")


# In-prompt cases: these overlap the prompt few-shots (ROUTING_EXAMPLES), so a high
# score here measures memorization, not generalization (see docs/eval.md). Exactly
# one defensible recipient per message. Messages are kept ASCII-only so the report
# prints cleanly on any console encoding; the model still routes by intent, not by
# exact wording.
CASES = [
    ("Chcialbym zglosic urlop na jutro", "kadry@example.com"),
    ("Potrzebuje zaswiadczenia o zatrudnieniu", "kadry@example.com"),
    ("Nie zgadza mi sie wynagrodzenie", "kadry@example.com"),
    ("Mam pytanie o proces rekrutacji", "human-resources@example.com"),
    ("Chcialbym zapytac o benefity pracownicze", "human-resources@example.com"),
    ("Nie dziala mi komputer", "help-desk@example.com"),
    ("Drukarka nie drukuje", "help-desk@example.com"),
    ("Laptop nie chce sie uruchomic", "help-desk@example.com"),
    ("Nie moge zalogowac sie do platformy szkoleniowej", "it@example.com"),
    ("Aplikacja zwraca blad 500", "it@example.com"),
    ("Mam sprawe organizacyjna", "other@example.com"),
]

# Held-out cases: NONE of these appear in ROUTING_EXAMPLES (the prompt's few-shots).
# Different wording, jargon, typos and novel intents, so this set measures
# generalization instead of the model echoing its own prompt. ASCII-only for clean
# console output. test_held_out_cases_do_not_overlap_prompt enforces the no-overlap.
HELD_OUT_CASES = [
    ("Od kiedy nalicza sie ekwiwalent za niewykorzystany urlop", "kadry@example.com"),
    ("Prosze o korekte liczby nadgodzin w grafiku za maj", "kadry@example.com"),
    ("Czy moj aneks do umowy jest juz podpisany", "kadry@example.com"),
    ("Chcialbym polecic znajomego na otwarty wakat", "human-resources@example.com"),
    ("Jak zapisac sie na szkolenie z przywodztwa", "human-resources@example.com"),
    ("Czy karta sportowa obejmuje osobe towarzyszaca", "human-resources@example.com"),
    ("Mysz bezprzewodowa nie reaguje po wymianie baterii", "help-desk@example.com"),
    ("Zaciecie papieru w drukarce na trzecim pietrze", "help-desk@example.com"),
    ("Drugi monitor pokazuje tylko czarny ekran", "help-desk@example.com"),
    ("Po resecie hasla nadal dostaje access denied w CRM", "it@example.com"),
    ("Potrzebuje uprawnien do repozytorium git zespolu", "it@example.com"),
    ("Integracja z API platnosci zwraca blad 401", "it@example.com"),
    ("Gdzie moge zaparkowac rower na terenie biura", "other@example.com"),
    ("Mam pytanie o parking dla gosci", "other@example.com"),
]

# Boundary cases: the task intentionally leaves these overlaps undefined, so more
# than one recipient is defensible. They probe the exact seams the recruiters made
# similar on purpose; any recipient in the acceptable set counts as resolved and
# none counts as a failure.
BOUNDARY_CASES: list[tuple[str, tuple[str, ...]]] = [
    (
        "Pytanie o onboarding nowej osoby",
        ("human-resources@example.com", "kadry@example.com"),
    ),
    (
        "Nie mam dostepu do sluzbowego laptopa",
        ("help-desk@example.com", "it@example.com"),
    ),
    (
        "Aplikacja na sluzbowym laptopie ciagle sie zawiesza",
        ("help-desk@example.com", "it@example.com"),
    ),
    (
        "Mam pytanie o benefity i o ostatnia wyplate",
        ("human-resources@example.com", "kadry@example.com", "other@example.com"),
    ),
    (
        "Nowy pracownik potrzebuje konta w systemie i sluzbowego laptopa",
        (
            "human-resources@example.com",
            "it@example.com",
            "help-desk@example.com",
            "other@example.com",
        ),
    ),
]


def route(message: str) -> dict[str, Any]:
    payload = json.dumps({"email": SENDER_EMAIL, "message": message}, ensure_ascii=False).encode(
        "utf-8"
    )
    require_http_url(ROUTE_URL)
    request = urllib.request.Request(
        ROUTE_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(  # nosec B310
        request, timeout=REQUEST_TIMEOUT_SECONDS
    ) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: API did not return a JSON object for {message!r}")
    return data


def route_message(message: str) -> dict[str, Any]:
    try:
        return route(message)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"ERROR: API returned HTTP {exc.code} for {message!r}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"ERROR: API call failed for {message!r}: {exc.reason}") from exc
    except OSError as exc:
        # Read timeouts, connection resets and other socket errors urllib does
        # not wrap in URLError.
        raise SystemExit(f"ERROR: API call failed for {message!r}: {exc}") from exc


def _run_clear_cut_set(title: str, cases: list[tuple[str, str]]) -> None:
    hits = 0
    fallbacks = 0
    total = len(cases)

    print(title)
    for message, expected in cases:
        if expected not in ALLOWED_RECIPIENTS:
            raise SystemExit(f"Test setup error: {expected} is not an allowed recipient")

        result = route_message(message)
        recipient = result.get("recipient")
        used_fallback = bool(result.get("used_fallback"))
        if used_fallback:
            fallbacks += 1

        if recipient == expected:
            hits += 1
            verdict = "HIT"
        elif used_fallback:
            verdict = "FALLBACK"
        else:
            verdict = "MISS"

        print(f"[{verdict:11}] expected={expected:28} got={recipient} :: {message}")

    print()
    print(f"Hits: {hits}/{total}   Fallbacks: {fallbacks}/{total}")


def run_clear_cut_cases() -> None:
    # These cases overlap the prompt examples. Treat them as a smoke check, not a
    # generalization score.
    _run_clear_cut_set(
        "In-prompt cases (overlap the prompt's routing examples; memorization "
        "sanity-check, not a generalization score):",
        CASES,
    )
    print(
        "Note: a few defensible alternatives (for example help-desk vs IT) are "
        "expected and do not indicate a failure."
    )


def run_held_out_cases() -> None:
    # None of these appear in ROUTING_EXAMPLES, so this is the figure that actually
    # measures generalization.
    _run_clear_cut_set(
        "Held-out cases (not in the prompt; this is the generalization signal):",
        HELD_OUT_CASES,
    )


def run_boundary_cases() -> None:
    resolved = 0
    total = len(BOUNDARY_CASES)

    print("Boundary cases (undefined seams; any listed recipient is acceptable):")
    for message, acceptable in BOUNDARY_CASES:
        for candidate in acceptable:
            if candidate not in ALLOWED_RECIPIENTS:
                raise SystemExit(f"Test setup error: {candidate} is not an allowed recipient")

        result = route_message(message)
        recipient = result.get("recipient")
        used_fallback = bool(result.get("used_fallback"))

        if recipient in acceptable:
            resolved += 1
            verdict = "OK-FALLBACK" if used_fallback else "OK"
        elif used_fallback:
            verdict = "FALLBACK"
        else:
            verdict = "OFF-SEAM"

        accept_str = " | ".join(acceptable)
        print(f"[{verdict:11}] acceptable={accept_str} -> got={recipient} :: {message}")

    print()
    print(f"Boundary resolved within acceptable set: {resolved}/{total}")
    print(
        "Boundary cases never count as failures; they show how the model resolves "
        "the ambiguity the task deliberately leaves open."
    )


def main() -> int:
    print(f"Routing accuracy check against {API_BASE_URL}")
    print(f"Sender email: {SENDER_EMAIL}")
    print()

    run_clear_cut_cases()
    print()
    run_held_out_cases()
    print()
    run_boundary_cases()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
