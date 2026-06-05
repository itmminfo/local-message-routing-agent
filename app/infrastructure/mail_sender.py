import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import get_settings
from app.domain.departments import validate_department_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MailSendResult:
    sent: bool
    recipient: str
    reply_to: str
    subject: str


class MailSender:
    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        mail_from: str | None = None,
    ) -> None:
        settings = get_settings()
        self.smtp_host = smtp_host or settings.SMTP_HOST
        self.smtp_port = smtp_port or settings.SMTP_PORT
        self.mail_from = mail_from or settings.MAIL_FROM
        self.smtp_timeout_seconds = settings.SMTP_TIMEOUT_SECONDS

    def send_email(
        self,
        to: str,
        reply_to: str,
        subject: str,
        body: str,
    ) -> MailSendResult:
        recipient = validate_department_email(to).value
        self._reject_crlf("From", self.mail_from)
        self._reject_crlf("To", recipient)
        self._reject_crlf("Reply-To", reply_to)
        self._reject_crlf("Subject", subject)

        message = EmailMessage()
        message["From"] = self.mail_from
        message["To"] = recipient
        message["Reply-To"] = reply_to
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(
            self.smtp_host,
            self.smtp_port,
            timeout=self.smtp_timeout_seconds,
        ) as smtp:
            smtp.send_message(message)

        logger.info("SMTP email sent recipient=%s", recipient)

        return MailSendResult(
            sent=True,
            recipient=recipient,
            reply_to=reply_to,
            subject=subject,
        )

    def is_ready(self, timeout_seconds: float | None = None) -> bool:
        # Readiness probe: can an SMTP session be opened? `is None` (not `or`) honors an
        # explicit 0.0 instead of upgrading it to the long default.
        probe_timeout = self.smtp_timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            with smtplib.SMTP(
                self.smtp_host,
                self.smtp_port,
                timeout=probe_timeout,
            ) as smtp:
                smtp.noop()
        except (smtplib.SMTPException, OSError):
            return False
        return True

    @staticmethod
    def _reject_crlf(header_name: str, value: str) -> None:
        if "\r" in value or "\n" in value:
            raise ValueError(f"{header_name} header must not contain CRLF.")
