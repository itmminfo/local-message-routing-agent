import logging
import re
import smtplib
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Literal

from app.config import get_settings
from app.domain.departments import validate_department_email

logger = logging.getLogger(__name__)


class LocalMailpitRequired(ValueError):
    pass


class MailDeliveryError(RuntimeError):
    def __init__(self, phase: Literal["before_data", "unknown"], message: str) -> None:
        super().__init__(message)
        self.phase = phase


class MailPreparationError(MailDeliveryError, ValueError):
    """Validation/serialization failed before any SMTP connection was attempted."""

    def __init__(self, message: str) -> None:
        MailDeliveryError.__init__(self, "before_data", message)


@dataclass(frozen=True)
class MailSendResult:
    sent: bool
    recipient: str
    reply_to: str
    subject: str
    message_id: str = ""


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

    def assert_local(self) -> None:
        if self.smtp_host != "mailpit" or self.smtp_port != 1025:
            raise LocalMailpitRequired("Local Mailpit SMTP is required.")

    def send_email(
        self,
        to: str,
        reply_to: str,
        subject: str,
        body: str,
        *,
        message_id: str,
    ) -> MailSendResult:
        self.assert_local()
        try:
            recipient, data = self._prepare_message(to, reply_to, subject, body, message_id)
        except Exception as exc:
            raise MailPreparationError(str(exc)) from exc
        smtp: smtplib.SMTP | None = None
        data_started = False
        acknowledged = False
        try:
            smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.smtp_timeout_seconds)
            smtp.ehlo_or_helo_if_needed()
            code, response = smtp.mail(self.mail_from)
            if code != 250:
                raise smtplib.SMTPSenderRefused(code, response, self.mail_from)
            code, response = smtp.rcpt(recipient)
            if code not in (250, 251):
                raise smtplib.SMTPRecipientsRefused({recipient: (code, response)})
            # DATA command itself has not transmitted the message body yet.
            code, response = smtp.docmd("DATA")
            if code != 354:
                raise smtplib.SMTPDataError(code, response)
            # From this point any transport failure may follow a partial/full DATA.
            data_started = True
            smtp.send(data)
            code, response = smtp.getreply()
            if code != 250:
                raise smtplib.SMTPDataError(code, response)
            acknowledged = True
        except Exception as exc:
            phase: Literal["before_data", "unknown"] = "unknown" if data_started else "before_data"
            raise MailDeliveryError(phase, "SMTP delivery was not confirmed.") from exc
        finally:
            if smtp is not None:
                if acknowledged:
                    try:
                        smtp.quit()
                    except (smtplib.SMTPException, OSError):
                        # Positive DATA ACK is authoritative; failed QUIT is not a retry.
                        logger.warning("SMTP QUIT failed after positive DATA acknowledgement")
                with suppress(OSError):
                    smtp.close()
        logger.info("Reviewed SMTP email accepted recipient=%s", recipient)
        return MailSendResult(True, recipient, reply_to, subject, message_id)

    def _prepare_message(
        self,
        to: str,
        reply_to: str,
        subject: str,
        body: str,
        message_id: str,
    ) -> tuple[str, bytes]:
        recipient = validate_department_email(to).value
        for name, value in [
            ("From", self.mail_from),
            ("To", recipient),
            ("Reply-To", reply_to),
            ("Subject", subject),
            ("Message-ID", message_id),
        ]:
            self._reject_crlf(name, value)
        if not re.fullmatch(r"<[A-Za-z0-9._-]+@[A-Za-z0-9.-]+>", message_id):
            raise ValueError("A stable backend Message-ID is required.")
        message = EmailMessage()
        message["From"] = self.mail_from
        message["To"] = recipient
        message["Reply-To"] = reply_to
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message.set_content(body)
        data = message.as_bytes(policy=SMTP)
        data = re.sub(rb"(?m)^\.", b"..", data)
        if not data.endswith(b"\r\n"):
            data += b"\r\n"
        data += b".\r\n"
        return recipient, data

    def is_ready(self, timeout_seconds: float | None = None) -> bool:
        if self.smtp_host != "mailpit" or self.smtp_port != 1025:
            return False
        probe_timeout = self.smtp_timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=probe_timeout) as smtp:
                smtp.noop()
        except (smtplib.SMTPException, OSError):
            return False
        return True

    @staticmethod
    def _reject_crlf(header_name: str, value: str) -> None:
        if "\r" in value or "\n" in value:
            raise ValueError(f"{header_name} header must not contain CRLF.")
