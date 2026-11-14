import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from app.agent.routing_agent import RoutingProposal
from app.infrastructure.mail_sender import MailDeliveryError, MailSender
from app.review.service import ReviewService
from app.review.store import ReviewStore


class RefusingModel:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, sender_email: str, message: str) -> RoutingProposal:
        self.calls += 1
        raise OSError("No model available")


class ControlledSender:
    smtp_host = "mailpit"
    smtp_port = 1025

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, str]] = []
        self.error: Exception | None = None

    def send_email(
        self, to: str, reply_to: str, subject: str, body: str, *, message_id: str
    ) -> object:
        self.calls.append((to, reply_to, subject, body, message_id))
        if self.error:
            raise self.error
        return object()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[ReviewService]:
    store = ReviewStore(tmp_path / "review.sqlite3")
    store.start()
    store.create_session("operator", "csrf", 3600)
    try:
        yield ReviewService(store, RefusingModel(), ControlledSender())
    finally:
        store.close()


@pytest.mark.parametrize("phase", ["before_data", "unknown", "unexpected"])
def test_delivery_failure_only_allows_manual_retry_before_submission(
    service: ReviewService, phase: str
) -> None:
    sender = service.mail_sender
    assert isinstance(sender, ControlledSender)
    sender.error = (
        OSError("unclassified failure")
        if phase == "unexpected"
        else MailDeliveryError(
            "before_data" if phase == "before_data" else "unknown", "Synthetic delivery failure"
        )
    )
    record = service.create_preview("operator", "person@example.com", "Original content")
    result = service.approve(
        record.id, "operator", record.version, record.payload_digest, "it@example.com"
    )
    assert result.state == (
        "failed_before_submit" if phase == "before_data" else "delivery_unknown"
    )
    assert result.retry_allowed is (phase == "before_data")
    assert len(sender.calls) == 1
    assert (
        service.approve(
            record.id, "operator", record.version, record.payload_digest, "it@example.com"
        ).state
        == result.state
    )
    assert len(sender.calls) == 1
    sender.error = None
    if phase == "before_data":
        sent = service.retry(
            record.id, "operator", record.version, record.payload_digest, result.attempts[-1].id
        )
        assert sent.state == "sent"
        assert len(sent.attempts) == 2
        assert sender.calls[0] == sender.calls[1]
    else:
        from app.review.store import ReviewConflict

        with pytest.raises(ReviewConflict):
            service.retry(
                record.id, "operator", record.version, record.payload_digest, result.attempts[-1].id
            )
        assert len(sender.calls) == 1


def test_model_result_arriving_after_approval_cannot_change_delivery(
    service: ReviewService,
) -> None:
    entered, release = threading.Event(), threading.Event()

    class SlowModel:
        def propose(self, sender_email: str, message: str) -> RoutingProposal:
            entered.set()
            assert release.wait(5)
            return RoutingProposal(
                "kadry@example.com", "Late suggestion", False, "test-model", "high", [message]
            )

    service.proposer = SlowModel()
    record = service.create_preview("operator", "person@example.com", "Original content")
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(service.suggest, record.id, "operator", record.version)
        try:
            assert entered.wait(5)
            sent = service.approve(
                record.id, "operator", record.version, record.payload_digest, "it@example.com"
            )
            assert sent.state == "sent"
        finally:
            release.set()
        late = future.result(timeout=5)
    assert late.recipient == "it@example.com"
    assert late.suggestion.recipient is None
    sender = service.mail_sender
    assert isinstance(sender, ControlledSender)
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "it@example.com"


def test_suggestion_capacity_does_not_block_manual_approval(service: ReviewService) -> None:
    record = service.create_preview("operator", "person@example.com", "Original content")
    slots = service.store.suggestion_slots
    acquired = 0
    try:
        while slots.acquire(blocking=False):
            acquired += 1
        result = service.suggest(record.id, "operator", record.version)
        assert result.suggestion.status == "error"
        assert result.state == "pending_review"
        assert isinstance(service.proposer, RefusingModel)
        assert service.proposer.calls == 0
        assert (
            service.approve(
                record.id, "operator", record.version, record.payload_digest, "it@example.com"
            ).state
            == "sent"
        )
    finally:
        for _ in range(acquired):
            slots.release()


def test_rejected_header_is_retryable_before_any_smtp_connection(service: ReviewService) -> None:
    service.mail_sender = MailSender(mail_from="bad\r\nBcc: attacker@example.com")
    record = service.create_preview("operator", "person@example.com", "Original content")
    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        result = service.approve(
            record.id, "operator", record.version, record.payload_digest, "it@example.com"
        )
    smtp_class.assert_not_called()
    assert result.state == "failed_before_submit"
    assert result.retry_allowed is True
    assert len(result.attempts) == 1


def test_retry_request_replayed_after_failure_and_restart_cannot_send_without_a_new_intent(
    service: ReviewService,
) -> None:
    sender = service.mail_sender
    assert isinstance(sender, ControlledSender)
    sender.error = MailDeliveryError("before_data", "SMTP did not receive DATA")
    original = service.create_preview("operator", "person@example.com", "Original content")
    first = service.approve(
        original.id, "operator", original.version, original.payload_digest, "it@example.com"
    )
    expected_id = first.attempts[-1].id
    second = service.retry(
        original.id, "operator", original.version, original.payload_digest, expected_id
    )
    assert second.state == "failed_before_submit"
    assert len(sender.calls) == 2
    service.store.close()
    restored = ReviewStore(service.store.path)
    restored.start()
    try:
        service.store = restored
        sender.error = None
        replay = service.retry(
            original.id, "operator", original.version, original.payload_digest, expected_id
        )
        assert replay.state == "failed_before_submit"
        assert len(replay.attempts) == 2
        assert len(sender.calls) == 2
        sent = service.retry(
            original.id,
            "operator",
            original.version,
            original.payload_digest,
            second.attempts[-1].id,
        )
        assert sent.state == "sent"
        assert len(sent.attempts) == 3
        assert len(sender.calls) == 3
        replay_after_sent = service.retry(
            original.id, "operator", original.version, original.payload_digest, expected_id
        )
        assert replay_after_sent.state == "sent"
        assert len(sender.calls) == 3
    finally:
        restored.close()
