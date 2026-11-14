import sqlite3
import subprocess
import sys
import threading
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
            restored.begin_retry(
                record.id, SESSION, record.version, record.payload_digest, attempt_id
            )
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
        return store.begin_retry(
            record.id, SESSION, record.version, record.payload_digest, attempt_id
        )

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


def test_same_retry_intent_is_not_repeated_after_fast_failure_and_restart(
    store: ReviewStore,
) -> None:
    record = preview(store)
    _, initial_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert initial_id is not None
    store.complete_attempt(record.id, SESSION, initial_id, "failed_before_submit")
    _, retry_id = store.begin_retry(
        record.id, SESSION, record.version, record.payload_digest, initial_id
    )
    assert retry_id is not None
    store.complete_attempt(record.id, SESSION, retry_id, "failed_before_submit")
    store.close()
    restored = ReviewStore(store.path)
    restored.start()
    try:
        replay, replay_id = restored.begin_retry(
            record.id, SESSION, record.version, record.payload_digest, initial_id
        )
        assert replay_id is None
        assert replay.state == "failed_before_submit"
        assert [a.id for a in replay.attempts] == [initial_id, retry_id]
        newest, fresh_retry_id = restored.begin_retry(
            record.id, SESSION, record.version, record.payload_digest, retry_id
        )
        assert fresh_retry_id is not None
        assert len(newest.attempts) == 3
        sent = restored.complete_attempt(record.id, SESSION, fresh_retry_id, "sent")
        assert sent.state == "sent"
        stale, stale_id = restored.begin_retry(
            record.id, SESSION, record.version, record.payload_digest, initial_id
        )
        assert stale_id is None
        assert [a.id for a in stale.attempts] == [a.id for a in sent.attempts]
        assert stale.state == "sent"
    finally:
        restored.close()


def test_concurrent_retry_of_one_failed_attempt_creates_one_successor_even_if_it_fails_fast(
    store: ReviewStore,
) -> None:
    record = preview(store)
    _, initial_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert initial_id is not None
    store.complete_attempt(record.id, SESSION, initial_id, "failed_before_submit")

    def retry_and_fail(_index: int) -> str | None:
        _, retry_id = store.begin_retry(
            record.id, SESSION, record.version, record.payload_digest, initial_id
        )
        if retry_id is not None:
            store.complete_attempt(record.id, SESSION, retry_id, "failed_before_submit")
        return retry_id

    with ThreadPoolExecutor(max_workers=8) as workers:
        attempts = list(workers.map(retry_and_fail, range(16)))
    assert sum(attempt is not None for attempt in attempts) == 1
    final = store.get_preview(record.id, SESSION)
    assert len(final.attempts) == 2
    assert final.state == "failed_before_submit"


@pytest.mark.parametrize(
    "source", ["unknown", "foreign_preview", "sending", "sent", "unknown_delivery"]
)
def test_retry_rejects_an_attempt_that_does_not_authorize_this_failed_preview(
    store: ReviewStore,
    source: str,
) -> None:
    record = preview(store)
    _, own_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert own_id is not None
    if source == "sending":
        expected_id = own_id
    elif source in {"sent", "unknown_delivery"}:
        expected_id = own_id
        store.complete_attempt(
            record.id,
            SESSION,
            own_id,
            "sent" if source == "sent" else "delivery_unknown",
        )
    else:
        store.complete_attempt(record.id, SESSION, own_id, "failed_before_submit")
        if source == "foreign_preview":
            other = preview(store)
            _, foreign_id = store.begin_approval(
                other.id, SESSION, other.version, other.payload_digest, RECIPIENT
            )
            assert foreign_id is not None
            expected_id = foreign_id
            store.complete_attempt(other.id, SESSION, expected_id, "failed_before_submit")
        else:
            expected_id = "f" * 32
    before = store.get_preview(record.id, SESSION)
    with pytest.raises(ReviewConflict):
        store.begin_retry(record.id, SESSION, record.version, record.payload_digest, expected_id)
    assert store.get_preview(record.id, SESSION) == before


def test_expired_preview_only_allows_observing_an_already_used_retry_intent(
    store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.review.store.time.time", lambda: 1000.0)
    record = preview(store)
    _, initial_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert initial_id is not None
    store.complete_attempt(record.id, SESSION, initial_id, "failed_before_submit")
    _, retry_id = store.begin_retry(
        record.id, SESSION, record.version, record.payload_digest, initial_id
    )
    assert retry_id is not None
    store.complete_attempt(record.id, SESSION, retry_id, "failed_before_submit")
    monkeypatch.setattr("app.review.store.time.time", lambda: 1060.0)
    expired_replay, attempt_id = store.begin_retry(
        record.id, SESSION, record.version, record.payload_digest, initial_id
    )
    assert attempt_id is None
    assert expired_replay.state == "expired"
    with pytest.raises(ReviewConflict):
        store.begin_retry(record.id, SESSION, record.version, record.payload_digest, retry_id)
    assert len(store.get_preview(record.id, SESSION).attempts) == 2


def test_rejection_fences_a_delayed_retry_and_survives_restart(store: ReviewStore) -> None:
    record = preview(store)
    _, initial_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert initial_id is not None
    failed = store.complete_attempt(record.id, SESSION, initial_id, "failed_before_submit")
    rejected = store.reject(record.id, SESSION, record.version)
    assert rejected.state == "rejected"
    assert rejected.attempts == failed.attempts
    assert rejected.message_id == failed.message_id
    assert rejected.recipient == failed.recipient
    store.close()
    restored = ReviewStore(store.path)
    restored.start()
    try:
        with pytest.raises(ReviewConflict):
            restored.begin_retry(
                record.id, SESSION, record.version, record.payload_digest, initial_id
            )
        # A delayed approval replay must not restart the withdrawn operation.
        late, late_id = restored.begin_approval(
            record.id, SESSION, record.version, record.payload_digest, RECIPIENT
        )
        assert late.state == "rejected"
        assert late_id is None
        assert late.attempts == failed.attempts
        assert restored.reject(record.id, SESSION, record.version) == rejected
        with sqlite3.connect(store.path) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM approvals WHERE preview_id=?", (record.id,)
                ).fetchone()[0]
                == 1
            )
    finally:
        restored.close()


def test_rejection_fences_a_delayed_first_approval_after_restart(store: ReviewStore) -> None:
    record = preview(store)
    store.reject(record.id, SESSION, record.version)
    store.close()
    restored = ReviewStore(store.path)
    restored.start()
    try:
        with pytest.raises(ReviewConflict):
            restored.begin_approval(
                record.id, SESSION, record.version, record.payload_digest, RECIPIENT
            )
        result = restored.get_preview(record.id, SESSION)
        assert result.state == "rejected"
        assert result.attempts == []
        assert result.message_id is None
    finally:
        restored.close()


@pytest.mark.parametrize("with_failure", [False, True])
def test_rejection_can_fence_an_expired_preview(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch, with_failure: bool
) -> None:
    monkeypatch.setattr("app.review.store.time.time", lambda: 1000.0)
    record = preview(store)
    if with_failure:
        _, attempt_id = store.begin_approval(
            record.id, SESSION, record.version, record.payload_digest, RECIPIENT
        )
        assert attempt_id is not None
        store.complete_attempt(record.id, SESSION, attempt_id, "failed_before_submit")
    monkeypatch.setattr("app.review.store.time.time", lambda: 1060.0)
    assert store.get_preview(record.id, SESSION).state == "expired"
    rejected = store.reject(record.id, SESSION, record.version)
    assert rejected.state == "rejected"
    assert rejected.retry_allowed is False
    assert store.reject(record.id, SESSION, record.version) == rejected


@pytest.mark.parametrize("state", ["sending", "sent", "delivery_unknown"])
def test_rejection_cannot_revoke_a_started_delivery(store: ReviewStore, state: str) -> None:
    record = preview(store)
    sending, attempt_id = store.begin_approval(
        record.id, SESSION, record.version, record.payload_digest, RECIPIENT
    )
    assert attempt_id is not None
    before = (
        sending
        if state == "sending"
        else store.complete_attempt(record.id, SESSION, attempt_id, state)
    )
    with pytest.raises(ReviewConflict):
        store.reject(record.id, SESSION, record.version)
    assert store.get_preview(record.id, SESSION) == before


@pytest.mark.parametrize("retry", [False, True])
def test_rejection_racing_delivery_has_one_atomic_winner(store: ReviewStore, retry: bool) -> None:
    for _ in range(16):
        record = preview(store)
        initial_id: str | None = None
        if retry:
            _, initial_id = store.begin_approval(
                record.id, SESSION, record.version, record.payload_digest, RECIPIENT
            )
            assert initial_id is not None
            store.complete_attempt(record.id, SESSION, initial_id, "failed_before_submit")
        barrier = threading.Barrier(2)

        def deliver(
            record: PreviewRecord = record,
            initial_id: str | None = initial_id,
            barrier: threading.Barrier = barrier,
        ) -> tuple[PreviewRecord, str | None] | None:
            barrier.wait(timeout=5)
            try:
                if retry:
                    assert initial_id is not None
                    return store.begin_retry(
                        record.id, SESSION, record.version, record.payload_digest, initial_id
                    )
                return store.begin_approval(
                    record.id, SESSION, record.version, record.payload_digest, RECIPIENT
                )
            except ReviewConflict:
                return None

        def reject(
            record: PreviewRecord = record, barrier: threading.Barrier = barrier
        ) -> PreviewRecord | None:
            barrier.wait(timeout=5)
            try:
                return store.reject(record.id, SESSION, record.version)
            except ReviewConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            delivery_result = workers.submit(deliver)
            rejection_result = workers.submit(reject)
            delivered, rejected = delivery_result.result(), rejection_result.result()
        final = store.get_preview(record.id, SESSION)
        if rejected is not None:
            assert delivered is None
            assert final.state == "rejected"
            assert len(final.attempts) == int(retry)
        else:
            assert delivered is not None and delivered[1] is not None
            assert final.state == "sending"
            assert len(final.attempts) == int(retry) + 1


def test_reject_closes_pending_suggestion_and_discards_late_result_after_restart(
    store: ReviewStore,
) -> None:
    record = preview(store)
    _, token = store.begin_suggestion(record.id, SESSION, record.version)
    assert token is not None
    rejected = store.reject(record.id, SESSION, record.version)
    assert rejected.state == "rejected"
    assert rejected.suggestion.status == "error"
    assert rejected.suggestion.error
    assert rejected.attempts == []
    assert rejected.recipient is None
    late = store.finish_suggestion(
        record.id,
        SESSION,
        record.version,
        token,
        Suggestion(status="ready", recipient=RECIPIENT, issue_quotes=[record.message]),
    )
    assert late == rejected
    store.close()
    restored = ReviewStore(store.path)
    restored.start()
    try:
        assert restored.get_preview(record.id, SESSION) == rejected
    finally:
        restored.close()


def test_expiry_closes_pending_suggestion_and_discards_late_result(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.review.store.time.time", lambda: 1000.0)
    record = preview(store)
    _, token = store.begin_suggestion(record.id, SESSION, record.version)
    assert token is not None
    monkeypatch.setattr("app.review.store.time.time", lambda: 1060.0)
    expired = store.get_preview(record.id, SESSION)
    assert expired.state == "expired"
    assert expired.suggestion.status == "error"
    assert expired.suggestion.error
    finished = store.finish_suggestion(
        record.id,
        SESSION,
        record.version,
        token,
        Suggestion(status="ready", recipient=RECIPIENT, issue_quotes=[record.message]),
    )
    assert finished == expired
    assert finished.attempts == []
    assert finished.recipient is None
    with sqlite3.connect(store.path) as db:
        suggestion_json, stored_token = db.execute(
            "SELECT suggestion_json,suggestion_token FROM previews WHERE id=?", (record.id,)
        ).fetchone()
    assert Suggestion.model_validate_json(suggestion_json) == finished.suggestion
    assert stored_token is None
    store.close()
    restored = ReviewStore(store.path)
    restored.start()
    try:
        assert restored.get_preview(record.id, SESSION) == expired
    finally:
        restored.close()


@pytest.mark.parametrize("closure", ["reject", "expire"])
@pytest.mark.parametrize("status", ["not_requested", "ready", "error"])
def test_closing_preview_preserves_an_already_finished_or_unrequested_suggestion(
    store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
    closure: str,
    status: str,
) -> None:
    monkeypatch.setattr("app.review.store.time.time", lambda: 1000.0)
    record = preview(store)
    if status != "not_requested":
        _, token = store.begin_suggestion(record.id, SESSION, record.version)
        assert token is not None
        suggestion = (
            Suggestion(status="ready", recipient=RECIPIENT, issue_quotes=[record.message])
            if status == "ready"
            else Suggestion(status="error", error="Original model error")
        )
        record = store.finish_suggestion(record.id, SESSION, record.version, token, suggestion)
    if closure == "reject":
        closed = store.reject(record.id, SESSION, record.version)
        assert closed.state == "rejected"
    else:
        monkeypatch.setattr("app.review.store.time.time", lambda: 1060.0)
        closed = store.get_preview(record.id, SESSION)
        assert closed.state == "expired"
    assert closed.suggestion == record.suggestion
    assert closed.attempts == []


def test_expired_suggestion_completion_still_requires_its_current_token(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.review.store.time.time", lambda: 1000.0)
    record = preview(store)
    _, old_token = store.begin_suggestion(record.id, SESSION, record.version)
    assert old_token is not None
    result = Suggestion(status="ready", recipient=RECIPIENT, issue_quotes=[record.message])
    store.finish_suggestion(record.id, SESSION, record.version, old_token, result)
    _, current_token = store.begin_suggestion(record.id, SESSION, record.version)
    assert current_token is not None and current_token != old_token
    monkeypatch.setattr("app.review.store.time.time", lambda: 1060.0)
    store.finish_suggestion(record.id, SESSION, record.version, old_token, result)
    with sqlite3.connect(store.path) as db:
        suggestion_json, stored_token = db.execute(
            "SELECT suggestion_json,suggestion_token FROM previews WHERE id=?", (record.id,)
        ).fetchone()
    assert stored_token == current_token
    assert Suggestion.model_validate_json(suggestion_json).status == "pending"
    closed = store.finish_suggestion(record.id, SESSION, record.version, current_token, result)
    assert closed.state == "expired"
    assert closed.suggestion.status == "error"
    assert closed.suggestion.recipient is None
