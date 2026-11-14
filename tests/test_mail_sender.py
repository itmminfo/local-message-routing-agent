import smtplib
from email import policy
from email.parser import BytesParser
from unittest.mock import MagicMock, patch

import pytest
from app.infrastructure.mail_sender import LocalMailpitRequired, MailDeliveryError, MailSender


def make_sender() -> MailSender:
    return MailSender(
        smtp_host="mailpit",
        smtp_port=1025,
        mail_from="no-reply@local-message-routing-agent.test",
    )


def test_send_email_requires_positive_data_ack() -> None:
    smtp = MagicMock()

    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.return_value = smtp
        smtp_class.return_value.__enter__.return_value = smtp
        smtp.mail.return_value = (250, b"OK")
        smtp.rcpt.return_value = (250, b"OK")
        smtp.docmd.return_value = (354, b"continue")
        smtp.getreply.return_value = (250, b"queued")

        result = make_sender().send_email(
            to="help-desk@example.com",
            reply_to="jan.nowak@example.com",
            subject="Message routed to help-desk",
            body="Nie działa mi komputer",
            message_id="<review-test@example.test>",
        )

    smtp_class.assert_called_once_with("mailpit", 1025, timeout=10)
    smtp.send.assert_called_once()
    smtp.getreply.assert_called_once()
    smtp.quit.assert_called_once()
    smtp.close.assert_called_once()
    assert result.sent is True
    assert result.recipient == "help-desk@example.com"


def test_reply_to_header_equals_original_sender_email() -> None:
    smtp = MagicMock()

    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.return_value = smtp
        smtp_class.return_value.__enter__.return_value = smtp
        smtp.mail.return_value = (250, b"OK")
        smtp.rcpt.return_value = (250, b"OK")
        smtp.docmd.return_value = (354, b"continue")
        smtp.getreply.return_value = (250, b"queued")

        make_sender().send_email(
            to="help-desk@example.com",
            reply_to="jan.nowak@example.com",
            subject="Message routed to help-desk",
            body="Nie działa mi komputer",
            message_id="<review-test@example.test>",
        )

    message = BytesParser(policy=policy.default).parsebytes(smtp.send.call_args.args[0][:-3])
    assert message["Reply-To"] == "jan.nowak@example.com"
    assert message["To"] == "help-desk@example.com"
    assert message["From"] == "no-reply@local-message-routing-agent.test"
    assert message["Subject"] == "Message routed to help-desk"
    assert message.get_content().replace("\r\n", "\n") == "Nie działa mi komputer\n"
    assert message["Message-ID"] == "<review-test@example.test>"


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
            message_id="<review-test@example.test>",
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
            message_id="<review-test@example.test>",
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
            message_id="<review-test@example.test>",
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
            message_id="<review-test@example.test>",
        )

    smtp_class.assert_not_called()


def test_is_ready_true_when_smtp_session_opens() -> None:
    smtp = MagicMock()

    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.return_value = smtp
        smtp_class.return_value.__enter__.return_value = smtp
        smtp.mail.return_value = (250, b"OK")
        smtp.rcpt.return_value = (250, b"OK")
        smtp.docmd.return_value = (354, b"continue")
        smtp.getreply.return_value = (250, b"queued")

        assert make_sender().is_ready() is True

    smtp.noop.assert_called_once()


def test_is_ready_false_when_smtp_connection_fails() -> None:
    with patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class:
        smtp_class.side_effect = OSError("connection refused")

        assert make_sender().is_ready() is False


@pytest.mark.parametrize(
    "phase", ["connect", "mail", "rcpt", "data-command", "send", "ack", "negative-ack"]
)
def test_smtp_failures_distinguish_before_body_from_possible_submission(phase: str) -> None:
    smtp = MagicMock()
    smtp.mail.return_value = (250, b"OK")
    smtp.rcpt.return_value = (250, b"OK")
    smtp.docmd.return_value = (354, b"continue")
    smtp.getreply.return_value = (250, b"queued")
    with patch("app.infrastructure.mail_sender.smtplib.SMTP", return_value=smtp) as smtp_class:
        if phase == "connect":
            smtp_class.side_effect = OSError("refused")
        elif phase == "mail":
            smtp.mail.return_value = (550, b"refused")
        elif phase == "rcpt":
            smtp.rcpt.return_value = (550, b"refused")
        elif phase == "data-command":
            smtp.docmd.return_value = (554, b"refused")
        elif phase == "send":
            smtp.send.side_effect = OSError("partial DATA")
        elif phase == "ack":
            smtp.getreply.side_effect = smtplib.SMTPServerDisconnected("lost DATA ACK")
        else:
            smtp.getreply.return_value = (554, b"DATA rejected")
        with pytest.raises(MailDeliveryError) as error:
            make_sender().send_email(
                "help-desk@example.com",
                "person@example.com",
                "Subject",
                "Body",
                message_id="<stable@example.test>",
            )
    assert error.value.phase == (
        "unknown" if phase in {"send", "ack", "negative-ack"} else "before_data"
    )
    if phase not in {"send", "ack", "negative-ack"}:
        smtp.send.assert_not_called()
    smtp.quit.assert_not_called()


def test_positive_data_ack_remains_success_when_quit_disconnects() -> None:
    smtp = MagicMock()
    smtp.mail.return_value = (250, b"OK")
    smtp.rcpt.return_value = (250, b"OK")
    smtp.docmd.return_value = (354, b"continue")
    smtp.getreply.return_value = (250, b"queued")
    smtp.quit.side_effect = smtplib.SMTPServerDisconnected("lost QUIT")
    with patch("app.infrastructure.mail_sender.smtplib.SMTP", return_value=smtp):
        result = make_sender().send_email(
            "help-desk@example.com",
            "person@example.com",
            "Subject",
            "Body",
            message_id="<stable@example.test>",
        )
    assert result.sent is True
    assert result.message_id == "<stable@example.test>"
    smtp.send.assert_called_once()
    smtp.close.assert_called_once()


@pytest.mark.parametrize(
    "host,port", [("smtp.example.com", 587), ("mailpit", 25), ("127.0.0.1", 1025)]
)
def test_non_mailpit_transport_is_rejected_before_connection(host: str, port: int) -> None:
    with (
        patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class,
        pytest.raises(LocalMailpitRequired),
    ):
        MailSender(smtp_host=host, smtp_port=port).send_email(
            "help-desk@example.com",
            "person@example.com",
            "Subject",
            "Body",
            message_id="<stable@example.test>",
        )
    smtp_class.assert_not_called()
