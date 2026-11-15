from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from app.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("valid_proposal", "extra_field"),
    [(True, None), (False, None), (False, "subject"), (False, "body"), (False, "reply_to")],
)
def test_real_dependency_graph_only_sends_after_explicit_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid_proposal: bool,
    extra_field: str | None,
) -> None:
    monkeypatch.setenv("REVIEW_DATABASE_PATH", str(tmp_path / "review.sqlite3"))
    get_settings.cache_clear()
    response = MagicMock()
    response.is_success = True
    routing: dict[str, Any] = {
        "message": {
            "tool_calls": [
                {
                    "function": {
                        "name": "propose_route",
                        "arguments": {
                            "category": "computing_hardware",
                            "reason": "Computer issue.",
                        },
                    }
                }
            ]
        }
    }
    if extra_field:
        routing["message"]["tool_calls"][0]["function"]["arguments"][extra_field] = {
            "subject": "HIJACKED\r\nBcc: attacker@example.com",
            "body": "SECRET-MODEL-PAYLOAD",
            "reply_to": "attacker@example.com",
        }[extra_field]
    response.json.side_effect = [
        {"message": {"content": '{"issue_quotes":["Nie działa komputer"]}'}},
        *(
            [routing]
            if valid_proposal
            else [routing if extra_field else {"message": {"content": "refusal"}}] * 2
        ),
    ]
    http = MagicMock()
    http.post.return_value = response
    smtp = MagicMock()
    smtp.mail.return_value = (250, b"OK")
    smtp.rcpt.return_value = (250, b"OK")
    smtp.docmd.return_value = (354, b"continue")
    smtp.getreply.return_value = (250, b"queued")
    with (
        patch("app.infrastructure.ollama_client.httpx.Client") as http_class,
        patch("app.infrastructure.mail_sender.smtplib.SMTP", return_value=smtp) as smtp_class,
    ):
        http_class.return_value.__enter__.return_value = http
        with TestClient(create_app(), base_url="http://localhost") as client:
            session = client.get("/api/v1/review/session").json()
            headers = {"Origin": "http://localhost", "X-CSRF-Token": session["csrf_token"]}
            preview = client.post(
                "/api/v1/review/previews",
                headers=headers,
                json={"email": "person@example.com", "message": "Nie działa komputer"},
            ).json()
            assert preview["state"] == "pending_review"
            http.post.assert_not_called()
            smtp_class.assert_not_called()
            path = "/api/v1/review/previews/" + preview["id"]
            suggested = client.post(
                path + "/suggest", headers=headers, json={"version": preview["version"]}
            ).json()
            assert suggested["suggestion"]["status"] == ("ready" if valid_proposal else "error")
            assert suggested["recipient"] is None
            smtp_class.assert_not_called()
            # The operator may deliberately choose another department than the model.
            approval = {
                "version": preview["version"],
                "payload_digest": preview["payload_digest"],
                "recipient": "it@example.com",
                "confirmed": True,
            }
            sent = client.post(path + "/approve", headers=headers, json=approval)
            assert sent.status_code == 200, sent.text
            assert sent.json()["state"] == "sent"
            assert (
                client.post(path + "/approve", headers=headers, json=approval).json()["state"]
                == "sent"
            )
    assert http.post.call_count == (2 if valid_proposal else 3)
    extraction = http.post.call_args_list[0].kwargs["json"]
    assert "format" in extraction and "tools" not in extraction
    assert "issue_quotes" in extraction["format"]["properties"]
    classification = http.post.call_args_list[1].kwargs["json"]
    assert classification["tools"][0]["function"]["name"] == "propose_route"
    assert set(classification["tools"][0]["function"]["parameters"]["properties"]) == {
        "category",
        "reason",
    }
    smtp.send.assert_called_once()
    message = BytesParser(policy=policy.default).parsebytes(smtp.send.call_args.args[0][:-3])
    assert message["To"] == "it@example.com"
    assert message["Reply-To"] == "person@example.com"
    assert message["Message-ID"] == sent.json()["message_id"]
    body = message.get_content().replace("\r\n", "\n")
    assert "Treść zgłoszenia:\nNie działa komputer\n\n" in body
    assert "Dział zatwierdzony przez operatora: it@example.com" in body
    for attack in ("HIJACKED", "SECRET-MODEL-PAYLOAD", "attacker@example.com"):
        assert attack not in str(message)
