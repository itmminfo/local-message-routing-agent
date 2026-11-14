import io
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from app.config import get_settings
from app.main import create_app
from app.observability import JsonLogFormatter
from app.review.routes import get_review_proposer, get_review_sender
from fastapi.testclient import TestClient

BASE = "/api/v1/review"
PAYLOAD = {"email": "outsider@gmail.com", "message": "Zażółć gęślą jaźń.\nKomputer nie działa."}


class NoModel:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, sender_email: str, message: str) -> object:
        self.calls += 1
        raise OSError("model deliberately unavailable")


class RecordingSender:
    smtp_host = "mailpit"
    smtp_port = 1025

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def send_email(
        self, to: str, reply_to: str, subject: str, body: str, *, message_id: str
    ) -> object:
        self.calls.append(
            {
                "to": to,
                "reply_to": reply_to,
                "subject": subject,
                "body": body,
                "message_id": message_id,
            }
        )
        self.entered.set()
        assert self.release.wait(5), "test did not release SMTP barrier"
        return object()


class ReviewHarness:
    def __init__(self, client: TestClient, sender: RecordingSender, model: NoModel) -> None:
        self.client = client
        self.sender = sender
        self.model = model
        response = client.get(f"{BASE}/session")
        assert response.status_code == 200
        self.session = response.json()
        self.headers = {"Origin": "http://localhost", "X-CSRF-Token": self.session["csrf_token"]}

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(f"{BASE}{path}", json=body, headers=self.headers)

    def create(self) -> dict[str, Any]:
        response = self.post("/previews", PAYLOAD)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    def approval(self, record: dict[str, Any], **changes: Any) -> dict[str, Any]:
        return {
            "version": record["version"],
            "payload_digest": record["payload_digest"],
            "recipient": "help-desk@example.com",
            "confirmed": True,
            **changes,
        }


@pytest.fixture
def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ReviewHarness]:
    monkeypatch.setenv("REVIEW_DATABASE_PATH", str(tmp_path / "review.sqlite3"))
    get_settings.cache_clear()
    sender, model = RecordingSender(), NoModel()
    target = create_app()
    target.dependency_overrides[get_review_sender] = lambda: sender
    target.dependency_overrides[get_review_proposer] = lambda: model
    with TestClient(target, base_url="http://localhost") as client:
        yield ReviewHarness(client, sender, model)


def test_create_preview_preserves_original_without_calling_model_or_smtp(
    review: ReviewHarness,
) -> None:
    record = review.create()
    assert record["state"] == "pending_review"
    assert record["email"] == PAYLOAD["email"]
    assert record["message"] == PAYLOAD["message"]
    assert record["recipient"] is None
    assert record["suggestion"]["status"] == "not_requested"
    assert review.model.calls == 0
    assert review.sender.calls == []
    assert (
        review.client.get(f"{BASE}/previews/{record['id']}").headers["cache-control"] == "no-store"
    )


def test_session_cookie_is_http_only_strict_and_resumed(review: ReviewHarness) -> None:
    first = review.client.get(f"{BASE}/session")
    assert first.json()["csrf_token"] == review.session["csrf_token"]
    review.client.cookies.clear()
    fresh = review.client.get(f"{BASE}/session")
    cookie = fresh.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert fresh.json()["csrf_token"] != review.session["csrf_token"]
    assert fresh.headers["cache-control"] == "no-store"


def test_foreign_origin_get_does_not_receive_cors_permission(review: ReviewHarness) -> None:
    response = review.client.get(f"{BASE}/session", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("streaming", [False, True])
def test_preview_body_limit_rejects_known_and_streamed_oversized_requests(
    review: ReviewHarness, streaming: bool
) -> None:
    body = b'{"email":"person@example.com","message":"' + b"x" * 200000 + b'"}'
    content = iter([body[:100000], body[100000:]]) if streaming else body
    response = review.client.post(
        f"{BASE}/previews",
        content=content,
        headers={**review.headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["x-request-id"]
    assert review.model.calls == 0
    assert review.sender.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        {"email": "bad", "message": "x"},
        {"email": "jań@example.com", "message": "x"},
        {"email": "jan@münchen.example", "message": "x"},
        {"email": "person@example.com", "message": " "},
        {"email": "person@example.com", "message": ""},
        {"email": "person@example.com", "message": "x" * 5001},
        {**PAYLOAD, "recipient": "it@example.com"},
    ],
)
def test_invalid_preview_never_reaches_model_or_sender(
    review: ReviewHarness, bad: dict[str, str]
) -> None:
    assert review.post("/previews", bad).status_code == 422
    assert review.sender.calls == []
    assert review.model.calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.test"},
        {"Origin": "http://localhost"},
        {"Origin": "http://localhost", "X-CSRF-Token": "wrong"},
    ],
)
def test_mutations_require_exact_origin_and_session_csrf(
    review: ReviewHarness, headers: dict[str, str]
) -> None:
    response = review.client.post(f"{BASE}/previews", json=PAYLOAD, headers=headers)
    assert response.status_code == 403
    assert review.sender.calls == []


@pytest.mark.parametrize("origin", [None, "http://evil.test", "http://localhost:9999"])
def test_valid_csrf_does_not_authorize_a_missing_or_foreign_origin(
    review: ReviewHarness, origin: str | None
) -> None:
    headers = {"X-CSRF-Token": review.session["csrf_token"]}
    if origin is not None:
        headers["Origin"] = origin
    response = review.client.post(f"{BASE}/previews", json=PAYLOAD, headers=headers)
    assert response.status_code == 403
    assert review.sender.calls == []


@pytest.mark.parametrize("host", ["evil.test", "testserver.evil.test", "testserver:9999"])
def test_unapproved_host_cannot_create_operator_session(review: ReviewHarness, host: str) -> None:
    assert review.client.get(f"{BASE}/session", headers={"Host": host}).status_code == 403


def test_foreign_session_cannot_read_or_approve_preview(review: ReviewHarness) -> None:
    record = review.create()
    own_cookies = dict(review.client.cookies)
    review.client.cookies.clear()
    other = review.client.get(f"{BASE}/session").json()
    headers = {"Origin": "http://localhost", "X-CSRF-Token": other["csrf_token"]}
    assert review.client.get(f"{BASE}/previews/{record['id']}").status_code == 404
    assert (
        review.client.post(
            f"{BASE}/previews/{record['id']}/approve", json=review.approval(record), headers=headers
        ).status_code
        == 404
    )
    assert review.sender.calls == []
    review.client.cookies.update(own_cookies)


def test_manual_approval_works_after_optional_model_failure(review: ReviewHarness) -> None:
    record = review.create()
    suggested = review.post(f"/previews/{record['id']}/suggest", {"version": record["version"]})
    assert suggested.status_code == 200
    assert suggested.json()["suggestion"]["status"] == "error"
    assert review.sender.calls == []
    approved = review.post(f"/previews/{record['id']}/approve", review.approval(record))
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "sent"
    assert review.sender.calls[0]["to"] == "help-desk@example.com"
    assert review.sender.calls[0]["reply_to"] == PAYLOAD["email"]
    assert PAYLOAD["message"] in review.sender.calls[0]["body"]


def test_review_rejects_non_mailpit_sender_before_persisting_approval(
    review: ReviewHarness,
) -> None:
    record = review.create()
    review.sender.smtp_host = "smtp.example.com"
    response = review.post(f"/previews/{record['id']}/approve", review.approval(record))
    assert response.status_code == 409
    assert review.sender.calls == []
    saved = review.client.get(f"{BASE}/previews/{record['id']}").json()
    assert saved["state"] == "pending_review"
    assert saved["attempts"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed": False},
        {"confirmed": 1},
        {"confirmed": "true"},
        {"version": 999},
        {"payload_digest": "0" * 64},
        {"recipient": "attacker@example.com"},
        {"email": "attacker@example.com"},
        {"message": "replaced"},
        {"reason": "fake"},
    ],
)
def test_approval_cannot_replace_saved_payload_or_bypass_confirmation(
    review: ReviewHarness, changes: dict[str, Any]
) -> None:
    record = review.create()
    response = review.post(f"/previews/{record['id']}/approve", review.approval(record, **changes))
    assert response.status_code in (409, 422), response.text
    assert review.sender.calls == []
    saved = review.client.get(f"{BASE}/previews/{record['id']}").json()
    assert saved["state"] == "pending_review"
    assert saved["email"] == PAYLOAD["email"]
    assert saved["message"] == PAYLOAD["message"]


def test_approval_replay_is_idempotent_and_recipient_change_is_conflict(
    review: ReviewHarness,
) -> None:
    record = review.create()
    path = f"/previews/{record['id']}/approve"
    first = review.post(path, review.approval(record))
    second = review.post(path, review.approval(record))
    changed = review.post(path, review.approval(record, recipient="it@example.com"))
    assert (first.status_code, second.status_code, changed.status_code) == (200, 200, 409)
    assert first.json()["state"] == second.json()["state"] == "sent"
    assert len(review.sender.calls) == 1


def test_concurrent_approval_only_starts_one_smtp_attempt(review: ReviewHarness) -> None:
    record = review.create()
    path = f"/previews/{record['id']}/approve"
    review.sender.release.clear()
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(review.post, path, review.approval(record))
        try:
            assert review.sender.entered.wait(5)
            second = workers.submit(review.post, path, review.approval(record))
            replay = second.result(timeout=5)
            assert replay.status_code == 200, replay.text
            assert replay.json()["state"] == "sending"
            assert len(review.sender.calls) == 1
        finally:
            review.sender.release.set()
        assert first.result(timeout=5).json()["state"] == "sent"
    assert len(review.sender.calls) == 1


def test_request_id_reaches_sync_suggestion_error_log(review: ReviewHarness) -> None:
    record = review.create()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("app.review.service")
    logger.addHandler(handler)
    try:
        response = review.client.post(
            f"{BASE}/previews/{record['id']}/suggest",
            json={"version": record["version"]},
            headers={**review.headers, "X-Request-ID": "sync-review-request-123"},
        )
    finally:
        logger.removeHandler(handler)
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "sync-review-request-123"
    assert '"request_id": "sync-review-request-123"' in stream.getvalue()
    assert "Review suggestion unavailable" in stream.getvalue()


def test_rejected_preview_cannot_be_approved(review: ReviewHarness) -> None:
    record = review.create()
    response = review.post(f"/previews/{record['id']}/reject", {"version": record["version"]})
    assert response.json()["state"] == "rejected"
    assert (
        review.post(f"/previews/{record['id']}/approve", review.approval(record)).status_code == 409
    )
    assert review.sender.calls == []
