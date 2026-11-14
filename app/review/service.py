import json
import logging
from typing import Protocol

from app.agent.issue_boundary import grounded_issue_quotes
from app.agent.routing_agent import RoutingProposal
from app.config import get_settings
from app.infrastructure.mail_sender import LocalMailpitRequired, MailDeliveryError
from app.review.models import PreviewRecord, Suggestion
from app.review.store import ReviewStore

logger = logging.getLogger(__name__)


class SupportsPropose(Protocol):
    def propose(self, sender_email: str, message: str) -> RoutingProposal: ...


class SupportsReviewedSender(Protocol):
    smtp_host: str
    smtp_port: int

    def send_email(
        self,
        to: str,
        reply_to: str,
        subject: str,
        body: str,
        *,
        message_id: str,
    ) -> object: ...


class ReviewService:
    def __init__(
        self,
        store: ReviewStore,
        proposer: SupportsPropose,
        mail_sender: SupportsReviewedSender,
    ) -> None:
        self.store = store
        self.proposer = proposer
        self.mail_sender = mail_sender

    def create_preview(self, session_key: str, email: str, message: str) -> PreviewRecord:
        # No model or mail object is called while persisting the original.
        return self.store.create_preview(
            session_key,
            email,
            message,
            get_settings().REVIEW_PREVIEW_TTL_SECONDS,
        )

    def suggest(self, preview_id: str, session_key: str, version: int) -> PreviewRecord:
        record, token = self.store.begin_suggestion(preview_id, session_key, version)
        if token is None:
            return record
        if not self.store.suggestion_slots.acquire(blocking=False):
            return self.store.finish_suggestion(
                preview_id,
                session_key,
                version,
                token,
                Suggestion(status="error", error="Model jest zajęty. Możesz wybrać dział ręcznie."),
            )
        try:
            proposal = self.proposer.propose(record.email, record.message)
            quotes = grounded_issue_quotes(
                {"message": {"content": json.dumps({"issue_quotes": proposal.issue_quotes})}},
                record.message,
            )
            suggestion = Suggestion(
                status="ready",
                recipient=proposal.recipient,
                reason=proposal.reason,
                model=proposal.model,
                routing_confidence=proposal.routing_confidence,
                used_fallback=proposal.used_fallback,
                issue_quotes=quotes,
            )
        except Exception as exc:
            logger.warning("Review suggestion unavailable exception_type=%s", type(exc).__name__)
            suggestion = Suggestion(
                status="error",
                error="Brak poprawnej propozycji modelu. Możesz wybrać dział ręcznie.",
            )
        finally:
            self.store.suggestion_slots.release()
        # CAS discards a late result after approval, rejection or expiration.
        return self.store.finish_suggestion(preview_id, session_key, version, token, suggestion)

    def _require_local_sender(self) -> None:
        if self.mail_sender.smtp_host != "mailpit" or self.mail_sender.smtp_port != 1025:
            raise LocalMailpitRequired("Local Mailpit SMTP is required.")

    def approve(
        self,
        preview_id: str,
        session_key: str,
        version: int,
        payload_digest: str,
        recipient: str,
    ) -> PreviewRecord:
        self._require_local_sender()
        record, attempt_id = self.store.begin_approval(
            preview_id,
            session_key,
            version,
            payload_digest,
            recipient,
        )
        return record if attempt_id is None else self._deliver(record, session_key, attempt_id)

    def retry(
        self,
        preview_id: str,
        session_key: str,
        version: int,
        payload_digest: str,
        expected_attempt_id: str,
    ) -> PreviewRecord:
        self._require_local_sender()
        record, attempt_id = self.store.begin_retry(
            preview_id,
            session_key,
            version,
            payload_digest,
            expected_attempt_id,
        )
        return record if attempt_id is None else self._deliver(record, session_key, attempt_id)

    def _deliver(self, record: PreviewRecord, session_key: str, attempt_id: str) -> PreviewRecord:
        # Immutable server record, not any client-supplied body, reaches SMTP.
        if record.recipient is None or record.message_id is None:
            raise RuntimeError("Approved delivery has no bound recipient or Message-ID.")
        self._require_local_sender()
        subject = f"Zgłoszenie skierowane do: {record.recipient.value}"
        body = (
            "Wiadomość przekazana po lokalnym zatwierdzeniu.\n\n"
            f"Nadawca: {record.email}\n"
            f"Dział zatwierdzony przez operatora: {record.recipient.value}\n\n"
            f"Treść zgłoszenia:\n{record.message}\n\n"
        )
        try:
            self.mail_sender.send_email(
                to=record.recipient.value,
                reply_to=record.email,
                subject=subject,
                body=body,
                message_id=record.message_id,
            )
        except MailDeliveryError as exc:
            if exc.phase == "before_data":
                state = "failed_before_submit"
                error = "Nie przekazano DATA. Możesz jawnie ponowić tę zatwierdzoną operację."
            else:
                state = "delivery_unknown"
                error = (
                    "Możliwe przyjęcie wiadomości. "
                    "Sprawdź Mailpit po Message-ID przed nową decyzją."
                )
            return self.store.complete_attempt(record.id, session_key, attempt_id, state, error)
        except Exception as exc:
            # An unexpected sender exception cannot prove that DATA was never sent.
            logger.warning("Reviewed delivery uncertain exception_type=%s", type(exc).__name__)
            return self.store.complete_attempt(
                record.id,
                session_key,
                attempt_id,
                "delivery_unknown",
                "Nie można potwierdzić dostarczenia. Sprawdź Mailpit po Message-ID.",
            )
        return self.store.complete_attempt(record.id, session_key, attempt_id, "sent")
