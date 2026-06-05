import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from app.main import app
from fastapi import FastAPI


def request_app(
    target_app: FastAPI,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    async def call_app() -> httpx.Response:
        transport = httpx.ASGITransport(app=target_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(call_app())


def ollama_tool_response(recipient: str) -> dict[str, Any]:
    return {
        "message": {
            "tool_calls": [
                {
                    "function": {
                        "name": "send_email",
                        "arguments": {
                            "recipient": recipient,
                            "subject": "Message routed to help-desk",
                            "body": "Please help with this workstation issue.",
                            "reason": "The message concerns a computer issue.",
                        },
                    }
                }
            ]
        }
    }


def test_route_message_wires_real_agent_ollama_client_and_mail_sender() -> None:
    # Exercises the real dependency wiring in app/api/routes.py end to end:
    # route -> MessageRoutingService -> RoutingAgent -> OllamaClient -> MailSender.
    # Only the two external boundaries (Ollama HTTP and SMTP) are mocked, so a
    # broken DI graph would fail here even though every layer passes its own
    # unit tests.
    ollama_response = MagicMock()
    ollama_response.is_success = True
    ollama_response.json.return_value = ollama_tool_response("help-desk@example.com")

    http_client = MagicMock()
    http_client.post.return_value = ollama_response

    smtp = MagicMock()

    with (
        patch("app.infrastructure.ollama_client.httpx.Client") as client_class,
        patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class,
    ):
        client_class.return_value.__enter__.return_value = http_client
        smtp_class.return_value.__enter__.return_value = smtp

        response = request_app(
            app,
            "POST",
            "/api/v1/messages/route",
            json={
                "email": "jan.nowak@example.com",
                "message": "Nie działa mi komputer",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "sent"
    assert body["recipient"] == "help-desk@example.com"
    assert body["reply_to"] == "jan.nowak@example.com"
    assert body["used_fallback"] is False

    # The real OllamaClient actually posted to the Ollama chat endpoint...
    http_client.post.assert_called_once()
    assert http_client.post.call_args.args[0].endswith("/api/chat")

    # ...and the real MailSender actually delivered the routed email over SMTP
    # with the application-controlled Reply-To, never a model-supplied value.
    smtp.send_message.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "help-desk@example.com"
    assert message["Reply-To"] == "jan.nowak@example.com"
    assert message["From"] == "no-reply@local-message-routing-agent.test"


def test_route_message_falls_back_to_other_when_model_returns_no_tool_call() -> None:
    # Same real wiring, but the model never emits a tool call. After one retry
    # the agent must fall back to other@example.com and still deliver an email.
    no_tool_response = MagicMock()
    no_tool_response.is_success = True
    no_tool_response.json.return_value = {"message": {"content": "Plain text answer"}}

    http_client = MagicMock()
    http_client.post.return_value = no_tool_response

    smtp = MagicMock()

    with (
        patch("app.infrastructure.ollama_client.httpx.Client") as client_class,
        patch("app.infrastructure.mail_sender.smtplib.SMTP") as smtp_class,
    ):
        client_class.return_value.__enter__.return_value = http_client
        smtp_class.return_value.__enter__.return_value = smtp

        response = request_app(
            app,
            "POST",
            "/api/v1/messages/route",
            json={
                "email": "jan.nowak@example.com",
                "message": "Nie działa mi komputer",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "sent"
    assert body["recipient"] == "other@example.com"
    assert body["used_fallback"] is True

    # One initial call plus one retry before the deterministic fallback.
    assert http_client.post.call_count == 2
    smtp.send_message.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "other@example.com"
    assert message["Reply-To"] == "jan.nowak@example.com"
