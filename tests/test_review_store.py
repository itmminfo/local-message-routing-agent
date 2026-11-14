import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.review.models import PreviewRecord, Suggestion
from app.review.store import ReviewConflict, ReviewStore

SESSION = "operator-session"
RECIPIENT = "help-desk@example.com"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ReviewStore]:
    target = ReviewStore(tmp_path / "review.sqlite3")
    target.start()
    target.create_session(SESSION, "csrf", 3600)
    try:
        yield target
    finally:
        target.close()


def preview(store: ReviewStore) -> PreviewRecord:
    return store.create_preview(SESSION, "person@example.com", "Oryginał.\nTreść", 60)


def test_session_and_pending_preview_survive_store_restart(store: ReviewStore) -> None:
    record = preview(store)
    store.close()
    restored = ReviewStore(store.path)
    restored.start()
    try:
        session = restored.get_session(SESSION)
        assert session is not None
        assert session["csrf_token"] == "csrf"
        assert restored.get_preview(record.id, SESSION) == record
    finally:
        restored.close()


def test_separate_process_cannot_own_live_database(store: ReviewStore) -> None:
    code = """import sys
from app.review.store import ReviewStore, ReviewStoreBusy
store=ReviewStore(sys.argv[1])
try:
    store.start()
except ReviewStoreBusy:
    print('blocked')
else:
    store.close()
    raise SystemExit('second backend acquired live database')
"""
    probe = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (probe.returncode, probe.stdout.strip()) == (0, "blocked"), probe.stderr


@pytest.mark.parametrize(
    "column,value",
    [
        ("email", "replaced@example.com"),
        ("message", "Changed original"),
        ("version", 2),
        ("payload_digest", "0" * 64),
    ],
)
def test_approval_rejects_tampered_saved_payload(
    store: ReviewStore, column: str, value: str | int
) -> None:
    record = preview(store)
    assert column in {"email", "message", "version", "payload_digest"}
    with sqlite3.connect(store.path) as db:
        db.execute(f"UPDATE previews SET {column}=? WHERE id=?", (value, record.id))
    with pytest.raises(ReviewConflict):
        store.begin_approval(record.id, SESSION, record.version, record.payload_digest, RECIPIENT)
    assert store.get_preview(record.id, SESSION).attempts == []


def test_late_suggestion_does_not_change_accepted_recipient(store: ReviewStore) -> None:
    record = preview(store)
    _, token = store.begin_suggestion(record.id, SESSION, record.version)
    assert token is not None
    approved, _ = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    result = store.finish_suggestion(
        record.id,
        SESSION,
        record.version,
        token,
        Suggestion(status="ready", recipient="it@example.com", reason="Late result"),
    )
    assert result.recipient == RECIPIENT
    assert result.state == "sending"
    assert result.suggestion == approved.suggestion
    assert result.suggestion.recipient is None


def test_approval_compare_and_swap_only_creates_one_attempt(store: ReviewStore) -> None:
    record = preview(store)

    def approve(_: int) -> tuple[PreviewRecord, str | None]:
        return store.begin_approval(
            record.id, SESSION, record.version, record.payload_digest, RECIPIENT
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(approve, range(16)))
    assert sum(attempt_id is not None for _, attempt_id in results) == 1
    assert len(store.get_preview(record.id, SESSION).attempts) == 1


def test_restart_turns_in_flight_attempt_into_unknown_without_retry(store: ReviewStore) -> None:
    record = preview(store)
    sending, attempt_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert attempt_id is not None
    store.close()
    restored = ReviewStore(store.path)
    restored.start()
    try:
        recovered = restored.get_preview(record.id, SESSION)
        assert recovered.state == "delivery_unknown"
        assert recovered.retry_allowed is False
        assert recovered.message_id == sending.message_id
        assert len(recovered.attempts) == 1
        assert recovered.attempts[0].state == "delivery_unknown"
        with pytest.raises(ReviewConflict):
            restored.begin_retry(record.id, SESSION, record.version, record.payload_digest)
        assert (
            restored.begin_approval(
                record.id, SESSION, record.version, record.payload_digest, RECIPIENT
            )[1]
            is None
        )
    finally:
        restored.close()


def test_retry_compare_and_swap_preserves_approved_payload_and_message_id(
    store: ReviewStore,
) -> None:
    record = preview(store)
    sending, attempt_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert attempt_id is not None
    store.complete_attempt(record.id, SESSION, attempt_id, "failed_before_submit")

    def retry(_: int) -> tuple[PreviewRecord, str | None]:
        return store.begin_retry(record.id, SESSION, record.version, record.payload_digest)

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(retry, range(16)))
    assert sum(attempt is not None for _, attempt in results) == 1
    final = store.get_preview(record.id, SESSION)
    assert len(final.attempts) == 2
    assert {a.message_id for a in final.attempts} == {sending.message_id}
    assert (final.email, final.message, final.recipient, final.payload_digest) == (
        record.email,
        record.message,
        RECIPIENT,
        record.payload_digest,
    )


def test_expired_preview_cannot_begin_delivery(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr("app.review.store.time.time", lambda: 1000.0)
    record = preview(store)
    monkeypatch.setattr("app.review.store.time.time", lambda: 1060.0)
    assert store.get_preview(record.id, SESSION).state == "expired"
    with pytest.raises(ReviewConflict):
        store.begin_approval(record.id, SESSION, record.version, record.payload_digest, RECIPIENT)
    assert store.get_preview(record.id, SESSION).attempts == []


def test_expired_session_is_not_recovered(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr("app.review.store.time.time", lambda: 1000.0)
    store.create_session("short-session", "short-csrf", 10)
    monkeypatch.setattr("app.review.store.time.time", lambda: 1010.0)
    assert store.get_session("short-session") is None
