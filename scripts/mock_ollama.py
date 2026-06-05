#!/usr/bin/env python3
"""Deterministic in-process stand-in for the Ollama chat API, used by the CI E2E job.

This is NOT a model. It is a keyword stub that mimics just enough of the Ollama HTTP
API for the whole container pipeline to run in CI without pulling a multi-GB model:

- GET  /api/tags  -> reports the configured model as present, so ollama-init's
                     readiness/warmup loop and the API's /ready model check both pass.
- POST /api/chat  -> returns a single send_email tool call whose recipient is picked
                     by a small Polish keyword map over the last user message.

Because it is deterministic, the CI E2E run proves the *plumbing* (request -> tool
call -> validated recipient -> SMTP -> Mailpit, with correct Reply-To and header
handling) and that the pipeline faithfully carries the chosen recipient all the way
to the Mailpit To header. It does NOT measure real model routing accuracy; that is
non-deterministic and is measured separately against a real model (see docs/eval.md
and scripts/routing_accuracy_check.py).
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Kept in sync with app/domain/departments.py. The mock is intentionally standalone
# (it runs in a bare python:3.12-slim container with only this file bind-mounted, no
# app package), so the addresses are duplicated here rather than imported.
# tests/test_mock_ollama.py asserts they stay a subset of the real ALLOWED_RECIPIENTS
# so this duplication cannot silently drift outside the allowed set.
HUMAN_RESOURCES = "human-resources@example.com"
HELP_DESK = "help-desk@example.com"
IT = "it@example.com"
KADRY = "kadry@example.com"
OTHER = "other@example.com"

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
PORT = int(os.environ.get("OLLAMA_MOCK_PORT", "11434"))

# Ordered keyword map: the first recipient with a matching keyword wins. Keywords are
# matched against the diacritics-folded, lower-cased last user message, so "komputer"
# matches "Nie działa mi komputer". This is a coarse stub, not the routing policy; it
# only needs to resolve obvious single-intent messages for the deterministic E2E.
KEYWORD_ROUTING: list[tuple[str, tuple[str, ...]]] = [
    (
        KADRY,
        (
            "urlop",
            "zwolnieni",
            "l4",
            "wynagrodz",
            "wyplat",
            "pensj",
            "umow",
            "zaswiadcz",
            "nadgodzin",
            "grafik",
            "ekwiwalent",
            "aneks",
        ),
    ),
    (
        HUMAN_RESOURCES,
        (
            "rekrutacj",
            "kandyd",
            "benefit",
            "onboarding",
            "szkoleni",
            "rozwoj",
            "kariera",
            "feedback",
            "sportow",
            "wakat",
        ),
    ),
    (
        HELP_DESK,
        (
            "komputer",
            "laptop",
            "monitor",
            "drukark",
            "skaner",
            "klawiatur",
            "mysz",
            "ekran",
            "sprzet",
            "stanowisk",
        ),
    ),
    (
        IT,
        (
            "zalogow",
            "login",
            "haslo",
            "hasla",
            "konto",
            "dostep",
            "uprawnieni",
            "aplikacj",
            "platform",
            "system",
            "api",
            "integracj",
            "blad",
            "serwer",
            "baza danych",
            "crm",
            "repozytorium",
        ),
    ),
]


def _fold(text: str) -> str:
    # Strip Polish diacritics and lower-case so keyword matching is robust to "działa"
    # vs "dziala" and to capitalization.
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def classify(message: str) -> str:
    folded = _fold(message)
    for recipient, keywords in KEYWORD_ROUTING:
        if any(keyword in folded for keyword in keywords):
            return recipient
    return OTHER


def _last_user_message(payload: dict[str, Any]) -> str:
    # Classify only the user turn. The system prompt embeds the whole routing policy
    # and few-shot examples (every department's keywords, in both languages), so
    # scanning it would match arbitrarily; the user content holds just the message.
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for entry in reversed(messages):
        if isinstance(entry, dict) and entry.get("role") == "user":
            content = entry.get("content")
            if isinstance(content, str):
                return content
    return ""


def build_tags_response() -> dict[str, Any]:
    # The /api/tags shape Ollama returns. Both "name" and "model" carry the tag so the
    # API's _model_in_tags and the init script's grep for "<model>" both match.
    return {"models": [{"name": MODEL, "model": MODEL}]}


def build_chat_response(payload: dict[str, Any]) -> dict[str, Any]:
    recipient = classify(_last_user_message(payload))
    return {
        "model": MODEL,
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "send_email",
                        "arguments": {
                            "recipient": recipient,
                            "reason": "Mock routing stub (CI): keyword-selected recipient.",
                        },
                    }
                }
            ],
        },
        "done": True,
    }


class MockOllamaHandler(BaseHTTPRequestHandler):
    server_version = "MockOllama/1.0"

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.startswith("/api/tags"):
            self._send_json(200, build_tags_response())
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/api/chat"):
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        payload: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
        self._send_json(200, build_chat_response(payload))

    def log_message(self, format: str, *args: Any) -> None:
        # One concise line to stderr instead of the noisy default, so CI logs stay
        # readable while still showing each request the mock served.
        sys.stderr.write(f"mock-ollama {self.command} {self.path}\n")


def main() -> int:
    # Bind on all interfaces so the other compose services can reach the mock over the
    # CI-only compose network. nosec B104: a throwaway CI stub on an internal network,
    # never shipped to or exposed in production.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), MockOllamaHandler)  # nosec B104
    sys.stderr.write(f"mock-ollama listening on :{PORT} model={MODEL}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
