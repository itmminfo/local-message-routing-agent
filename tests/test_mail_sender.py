from unittest.mock import MagicMock, patch

import pytest
from app.infrastructure.mail_sender import MailSender


def make_sender() -> MailSender:
    return MailSender(
        smtp_host="mailpit",
        smtp_port=1025,
        mail_from="no-reply@local-message-routing-agent.test",
    )


def test_send_email_calls_send_message() -> None:
    smtp = MagicMock()

    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.return_value.__enter__.return_value = smtp

        result = make_sender().send_email(
            to="help-desk@example.com",
            reply_to="jan.nowak@example.com",
            subject="Message routed to help-desk",
            body="Nie działa mi komputer",
        )

    smtp_class.assert_called_once_with("mailpit", 1025, timeout=10)
    smtp.send_message.assert_called_once()
    assert result.sent is True
    assert result.recipient == "help-desk@example.com"


def test_reply_to_header_equals_original_sender_email() -> None:
    smtp = MagicMock()

    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.return_value.__enter__.return_value = smtp

        make_sender().send_email(
            to="help-desk@example.com",
            reply_to="jan.nowak@example.com",
            subject="Message routed to help-desk",
            body="Nie działa mi komputer",
        )

    message = smtp.send_message.call_args.args[0]
    assert message["Reply-To"] == "jan.nowak@example.com"
    assert message["To"] == "help-desk@example.com"
    assert message["From"] == "no-reply@local-message-routing-agent.test"
    assert message["Subject"] == "Message routed to help-desk"


def test_invalid_recipient_raises_before_smtp_connection() -> None:
    with (
        patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class,
        pytest.raises(ValueError, match="Unknown department recipient"),
    ):
        make_sender().send_email(
            to="attacker@example.com",
            reply_to="jan.nowak@example.com",
            subject="Message routed to help-desk",
            body="Nie działa mi komputer",
        )

    smtp_class.assert_not_called()


def test_subject_with_crlf_is_rejected() -> None:
    with (
        patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class,
        pytest.raises(ValueError, match="Subject header must not contain CRLF"),
    ):
        make_sender().send_email(
            to="help-desk@example.com",
            reply_to="jan.nowak@example.com",
            subject="Safe subject\r\nBcc: attacker@example.com",
            body="Nie działa mi komputer",
        )

    smtp_class.assert_not_called()


def test_reply_to_with_crlf_is_rejected() -> None:
    with (
        patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class,
        pytest.raises(ValueError, match="Reply-To header must not contain CRLF"),
    ):
        make_sender().send_email(
            to="help-desk@example.com",
            reply_to="jan.nowak@example.com\r\nBcc: attacker@example.com",
            subject="Safe subject",
            body="Nie działa mi komputer",
        )

    smtp_class.assert_not_called()


def test_from_with_crlf_is_rejected() -> None:
    sender = MailSender(
        smtp_host="mailpit",
        smtp_port=1025,
        mail_from="no-reply@test\r\nBcc: attacker@example.com",
    )
    with (
        patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class,
        pytest.raises(ValueError, match="From header must not contain CRLF"),
    ):
        sender.send_email(
            to="help-desk@example.com",
            reply_to="jan.nowak@example.com",
            subject="Safe subject",
            body="Nie działa mi komputer",
        )

    smtp_class.assert_not_called()


def test_is_ready_true_when_smtp_session_opens() -> None:
    smtp = MagicMock()

    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.return_value.__enter__.return_value = smtp

        assert make_sender().is_ready() is True

    smtp.noop.assert_called_once()


def test_is_ready_false_when_smtp_connection_fails() -> None:
    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.side_effect = OSError("connection refused")

        assert make_sender().is_ready() is False
