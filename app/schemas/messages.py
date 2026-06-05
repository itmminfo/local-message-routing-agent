from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.config import get_settings
from app.domain.departments import DepartmentEmail

MESSAGE_MAX_LENGTH = get_settings().MESSAGE_MAX_LENGTH


class MessageRouteRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "email": "jan.nowak@example.com",
                    "message": "Nie działa mi komputer",
                }
            ]
        }
    )

    email: EmailStr
    message: str = Field(min_length=1, max_length=MESSAGE_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def email_must_be_ascii(cls, value: str) -> str:
        # EmailStr accepts internationalized (EAI) addresses with Unicode in the
        # local-part or domain, but they cannot be placed in a standards-compliant
        # Reply-To header without SMTPUTF8/punycode, which this PoC does not
        # implement. Reject them explicitly instead of emitting a malformed
        # Reply-To header downstream.
        if not value.isascii():
            raise ValueError("internationalized email addresses (EAI) are not supported")
        return value

    @field_validator("message")
    @classmethod
    def message_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be whitespace only")
        return value


class MessageRouteResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "sent",
                    "recipient": "help-desk@example.com",
                    "reply_to": "jan.nowak@example.com",
                    "subject": "Zgłoszenie skierowane do: help-desk@example.com",
                    "reason": "Wiadomość dotyczy problemu ze sprzętem komputerowym.",
                    "used_fallback": False,
                    "model": "qwen2.5:3b",
                    "routing_confidence": "high",
                }
            ]
        }
    )

    status: Literal["sent"]
    recipient: DepartmentEmail
    reply_to: EmailStr
    subject: str
    reason: str
    used_fallback: bool
    model: str
    routing_confidence: Literal["high", "low"]


class ErrorResponse(BaseModel):
    """Error body for handled failures (FastAPI's HTTPException `detail` shape).

    No shared example: the handled statuses return different `detail` strings.
    """

    detail: str
