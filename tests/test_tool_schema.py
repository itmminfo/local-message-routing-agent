from typing import Any, cast

from app.agent.tool_schema import build_send_email_tool_schema
from app.domain.departments import ALLOWED_RECIPIENTS


def get_tool_parameters() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        build_send_email_tool_schema()["function"]["parameters"],
    )


def test_tool_name_is_send_email() -> None:
    tool = build_send_email_tool_schema()

    assert tool["type"] == "function"
    assert tool["function"]["name"] == "send_email"


def test_recipient_enum_contains_exactly_allowed_recipients() -> None:
    parameters = get_tool_parameters()
    recipient = parameters["properties"]["recipient"]

    assert recipient["type"] == "string"
    assert set(recipient["enum"]) == ALLOWED_RECIPIENTS


def test_reply_to_is_not_present_in_schema() -> None:
    parameters = get_tool_parameters()

    assert "reply_to" not in parameters["properties"]
    assert "reply_to" not in parameters.get("required", [])


def test_schema_exposes_only_recipient_and_reason() -> None:
    # The backend builds the subject and the body template; the model provides only
    # recipient and a short reason. So the schema exposes exactly those two fields,
    # with no subject, body, Reply-To, or other delivery-level arguments to ignore.
    parameters = get_tool_parameters()

    assert set(parameters["properties"]) == {"recipient", "reason"}
    assert parameters["required"] == ["recipient", "reason"]
    assert "subject" not in parameters["properties"]
    assert "body" not in parameters["properties"]
