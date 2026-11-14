import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest
from app.review.models import PreviewRecord
from app.review.store import ReviewStore

SESSION = "snapshot-operator"
RECIPIENT = "it@example.com"


@pytest.fixture(params=["delete", "wal"])
def snapshot_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ReviewStore]:
    """Exercise both production DELETE locking and concurrent WAL commits."""
    journal_mode: Literal["delete", "wal"] = request.param
    store = ReviewStore(tmp_path / "snapshot.sqlite3")
    store.start()
    try:
        with closing(sqlite3.connect(store.path)) as db:
            actual = db.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
            assert actual == journal_mode
        store.create_session(SESSION, "snapshot-csrf", 3600)
        yield store
    finally:
        store.close()


def overlapping_get(
    store: ReviewStore,
    preview: PreviewRecord,
    writer: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> PreviewRecord:
    """Pause the real reader after its preview SELECT, without replacing any SQL."""
    row_read = threading.Event()
    writer_finished = threading.Event()
    original_record = store._record

    def pause_reader(db: sqlite3.Connection, row: sqlite3.Row) -> PreviewRecord:
        if threading.current_thread().name.startswith("snapshot-reader"):
            row_read.set()
            # In DELETE mode a correct snapshot blocks the writer's commit.
            # Bound the wait so the reader releases its snapshot, then let the
            # writer finish. WAL mode commits while the reader remains paused.
            writer_finished.wait(2)
        return original_record(db, row)

    def mutate() -> None:
        assert row_read.wait(2), "reader did not reach its first SELECT"
        writer()
        writer_finished.set()

    monkeypatch.setattr(store, "_record", pause_reader)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-reader") as reader,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-writer") as mutator,
    ):
        read_result = reader.submit(store.get_preview, preview.id, SESSION)
        write_result = mutator.submit(mutate)
        observed = read_result.result(timeout=5)
        write_result.result(timeout=5)
    assert writer_finished.is_set(), "writer failed to commit its final state"
    return observed


def test_get_cannot_mix_unapproved_preview_with_delivered_attempt(
    snapshot_store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = snapshot_store
    preview = store.create_preview(SESSION, "sender@example.com", "Original message", 3600)

    def deliver() -> None:
        _, attempt_id = store.begin_approval(
            preview.id, SESSION, preview.version, preview.payload_digest, RECIPIENT
        )
        assert attempt_id is not None
        store.complete_attempt(preview.id, SESSION, attempt_id, "sent")

    observed = overlapping_get(store, preview, deliver, monkeypatch)

    # The preview row was read before approval. Its attempts must come from
    # that same snapshot, even if the competing delivery has already committed.
    assert observed.state == "pending_review"
    assert observed.attempts == []
    assert observed.recipient is None
    assert observed.message_id is None
    committed = store.get_preview(preview.id, SESSION)
    assert committed.state == "sent"
    assert [attempt.state for attempt in committed.attempts] == ["sent"]


def test_get_cannot_mix_retryable_preview_with_unknown_delivery_attempt(
    snapshot_store: ReviewStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = snapshot_store
    preview = store.create_preview(SESSION, "sender@example.com", "Original message", 3600)
    _, first_id = store.begin_approval(
        preview.id, SESSION, preview.version, preview.payload_digest, RECIPIENT
    )
    assert first_id is not None
    failed = store.complete_attempt(preview.id, SESSION, first_id, "failed_before_submit")

    def lose_ack() -> None:
        _, retry_id = store.begin_retry(
            preview.id, SESSION, preview.version, preview.payload_digest, first_id
        )
        assert retry_id is not None
        store.complete_attempt(preview.id, SESSION, retry_id, "delivery_unknown")

    observed = overlapping_get(store, preview, lose_ack, monkeypatch)

    assert observed.state == "failed_before_submit"
    assert observed.retry_allowed is True
    assert [(attempt.id, attempt.state) for attempt in observed.attempts] == [
        (first_id, "failed_before_submit")
    ]
    assert observed.message_id == failed.message_id
    committed = store.get_preview(preview.id, SESSION)
    assert committed.state == "delivery_unknown"
    assert committed.retry_allowed is False
    assert [attempt.state for attempt in committed.attempts] == [
        "failed_before_submit",
        "delivery_unknown",
    ]
