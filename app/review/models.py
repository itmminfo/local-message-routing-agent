from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.departments import DepartmentEmail
from app.schemas.messages import MessageRouteRequest

ReviewState = Literal[
    "pending_review",
    "approved",
    "sending",
    "sent",
    "failed_before_submit",
    "delivery_unknown",
    "rejected",
    "expired",
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePreviewRequest(MessageRouteRequest):
    model_config = ConfigDict(extra="forbid")


class VersionRequest(StrictRequest):
    version: int = Field(ge=1, strict=True)


class PayloadVersionRequest(VersionRequest):
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class RetryRequest(PayloadVersionRequest):
    expected_attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ApproveRequest(PayloadVersionRequest):
    recipient: DepartmentEmail
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation_is_explicit_boolean(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("Explicit confirmation is required.")
        return True


class Suggestion(BaseModel):
    status: Literal["not_requested", "pending", "ready", "error"] = "not_requested"
    recipient: DepartmentEmail | None = None
    reason: str = ""
    model: str = ""
    routing_confidence: Literal["high", "low"] = "low"
    used_fallback: bool = False
    issue_quotes: list[str] = Field(default_factory=list)
    error: str | None = None


class AttemptRecord(BaseModel):
    id: str
    sequence: int
    state: ReviewState
    message_id: str
    created_at: str
    updated_at: str
    error: str | None = None


class PreviewRecord(BaseModel):
    id: str
    version: int
    payload_digest: str
    email: str
    message: str
    state: ReviewState
    created_at: str
    expires_at: str
    recipient: DepartmentEmail | None
    suggestion: Suggestion
    message_id: str | None
    attempts: list[AttemptRecord]
    retry_allowed: bool
    error: str | None


class DepartmentOption(BaseModel):
    recipient: DepartmentEmail
    label: str


class SessionResponse(BaseModel):
    csrf_token: str
    departments: list[DepartmentOption]
