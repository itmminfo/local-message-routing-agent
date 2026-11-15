import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from app.agent.issue_boundary import (
    build_issue_selection_request,
    grounded_issue_selection,
    issue_selection_fingerprint,
)
from app.agent.prompts import build_routing_messages, routing_prompt_fingerprint
from app.agent.tool_schema import build_send_email_tool_schema
from app.domain.departments import ALLOWED_RECIPIENTS, DepartmentEmail
from app.infrastructure.ollama_client import OllamaChatError


class ReviewRequiredError(RuntimeError):
    pass


RETRY_INSTRUCTION = "You must call the send_email tool exactly once. Do not answer in plain text."

# User-facing texts (API `reason` and the email body) are Polish, like the email
# template; log messages stay English.
DEFAULT_REASON = "Model nie podał uzasadnienia."

# Backend consumes exactly recipient+reason and ignores any other key the model may
# emit (e.g. subject/body/to). Extra keys are counted without logging their names:
# model-generated keys may themselves contain private message content.
ALLOWED_TOOL_ARGUMENT_KEYS = frozenset({"recipient", "reason"})

logger = logging.getLogger(__name__)


class SupportsChatWithTools(Protocol):
    """Minimal contract the agent needs from an Ollama-style chat client."""

    model: str

    def chat_with_schema(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class FallbackCategory(StrEnum):
    """Reason category for an agent fallback, carried for structured logging."""

    NO_TOOL_CALLS = "no_tool_calls"
    MISSING_SEND_EMAIL_TOOL = "missing_send_email_tool"
    MALFORMED_ARGUMENTS = "malformed_send_email_arguments"
    BLANK_RECIPIENT = "blank_send_email_recipient"
    INVALID_RECIPIENT = "invalid_send_email_recipient"


@dataclass(frozen=True)
class RoutingProposal:
    recipient: str
    reason: str
    used_fallback: bool
    model: str
    routing_confidence: Literal["high", "low"]
    issue_quotes: list[str]


@dataclass(frozen=True)
class SendEmailToolArgs:
    recipient: str
    reason: str
    reason_defaulted: bool = False
    # Never retain model-generated key names, which may contain message content.
    extra_field_count: int = 0


@dataclass(frozen=True)
class SendEmailToolParseResult:
    tool_args: SendEmailToolArgs | None
    invalid_reason: str = ""
    category: FallbackCategory | None = None


class RoutingAgent:
    def __init__(
        self,
        ollama_client: SupportsChatWithTools,
    ) -> None:
        self.ollama_client = ollama_client
        # Single source of truth for the model: the client owns it (it is what is
        # actually sent to Ollama). Reported in RouteResult.model and logs.
        self.model = ollama_client.model

    def propose(self, sender_email: str, message: str) -> RoutingProposal:
        logger.info(
            "Routing started model=%s prompt=%s issue_prompt=%s message_length=%d",
            self.model,
            routing_prompt_fingerprint(),
            issue_selection_fingerprint(),
            len(message),
        )

        selection_messages, selection_schema = build_issue_selection_request(message)
        selection = self.ollama_client.chat_with_schema(selection_messages, selection_schema)
        issue_quotes = grounded_issue_selection(selection, message)
        issue_text = "\n".join(issue_quotes)
        messages = build_routing_messages(sender_email=sender_email, message=issue_text)
        tools = [build_send_email_tool_schema()]

        response = self.ollama_client.chat_with_tools(messages, tools)
        parse_result = self._parse_send_email_tool_calls(response)
        tool_args = parse_result.tool_args

        retried = False
        if tool_args is None:
            retried = True
            retry_messages = [
                *messages,
                {"role": "system", "content": RETRY_INSTRUCTION},
            ]
            response = self.ollama_client.chat_with_tools(retry_messages, tools)
            parse_result = self._parse_send_email_tool_calls(response)
            tool_args = parse_result.tool_args

        if tool_args is None:
            category = parse_result.category or FallbackCategory.NO_TOOL_CALLS
            logger.info("Routing fallback used reason_category=%s", category.value)
            raise OllamaChatError("Model did not provide a valid routing proposal.")

        logger.info("Valid send_email tool call found recipient=%s", tool_args.recipient)
        if tool_args.extra_field_count:
            # Model-generated keys and values are untrusted and may contain message data.
            logger.warning(
                "send_email tool call included unsupported arguments ignored_count=%d",
                tool_args.extra_field_count,
            )
        if tool_args.reason_defaulted:
            # Valid recipient but no usable reason -> DEFAULT_REASON. Not a fallback;
            # logged separately to measure how often the reason is missing.
            logger.info(
                "Routing reason defaulted recipient=%s after_retry=%s",
                tool_args.recipient,
                retried,
            )
        return RoutingProposal(
            recipient=tool_args.recipient,
            reason=tool_args.reason,
            used_fallback=False,
            model=self.model,
            routing_confidence=(
                "low" if tool_args.recipient == DepartmentEmail.OTHER.value else "high"
            ),
            issue_quotes=issue_quotes,
        )

    def route(self, sender_email: str, message: str) -> RoutingProposal:
        raise ReviewRequiredError("Human review is required before delivery.")

    def _parse_send_email_tool_calls(
        self,
        response: dict[str, Any],
    ) -> SendEmailToolParseResult:
        tool_calls = self._extract_tool_calls(response)
        if not tool_calls:
            return SendEmailToolParseResult(
                tool_args=None,
                invalid_reason="Model nie wywołał żadnego narzędzia.",
                category=FallbackCategory.NO_TOOL_CALLS,
            )

        # Return the first send_email call that parses into a valid recipient. If none
        # do, report the last error seen: a malformed or invalid send_email call is more
        # useful in the fallback log than "no send_email call at all".
        last_error = SendEmailToolParseResult(
            tool_args=None,
            invalid_reason="Model nie wywołał narzędzia send_email.",
            category=FallbackCategory.MISSING_SEND_EMAIL_TOOL,
        )
        for tool_call in tool_calls:
            if self._tool_call_name(tool_call) != "send_email":
                continue

            arguments = self._parse_arguments(self._tool_call_arguments(tool_call))
            if arguments is None:
                last_error = SendEmailToolParseResult(
                    tool_args=None,
                    invalid_reason="Model zwrócił niepoprawne argumenty send_email.",
                    category=FallbackCategory.MALFORMED_ARGUMENTS,
                )
                continue

            parse_result = self._build_tool_args(arguments)
            if parse_result.tool_args is None:
                last_error = parse_result
                continue

            return parse_result

        return last_error

    @staticmethod
    def _extract_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
        message = response.get("message")
        if not isinstance(message, dict):
            # Ollama normally returns {"message": {...}}, but guard against an
            # unexpected shape (null/str/list) so a malformed response falls back
            # gracefully instead of raising AttributeError -> HTTP 500.
            message = {}
        tool_calls = message.get("tool_calls") or response.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            return []
        return [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]

    @staticmethod
    def _tool_call_name(tool_call: dict[str, Any]) -> str | None:
        function = tool_call.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str):
                return name
        name = tool_call.get("name")
        if isinstance(name, str):
            return name
        return None

    @staticmethod
    def _tool_call_arguments(tool_call: dict[str, Any]) -> Any:
        function = tool_call.get("function")
        if isinstance(function, dict) and "arguments" in function:
            return function["arguments"]
        return tool_call.get("arguments")

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> dict[str, Any] | None:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _build_tool_args(arguments: dict[str, Any]) -> SendEmailToolParseResult:
        recipient = arguments.get("recipient")
        if not isinstance(recipient, str) or not recipient.strip():
            return SendEmailToolParseResult(
                tool_args=None,
                invalid_reason="Model nie wskazał odbiorcy.",
                category=FallbackCategory.BLANK_RECIPIENT,
            )

        recipient = recipient.strip()
        if recipient not in ALLOWED_RECIPIENTS:
            return SendEmailToolParseResult(
                tool_args=None,
                invalid_reason=f"Model wskazał odbiorcę spoza listy: {recipient}.",
                category=FallbackCategory.INVALID_RECIPIENT,
            )

        # Blank/missing reason is defaulted (not rejected), so a correct recipient is not
        # downgraded over a secondary field; reason_defaulted records it for the log.
        reason_value = arguments.get("reason")
        if isinstance(reason_value, str) and reason_value.strip():
            reason = reason_value.strip()
            reason_defaulted = False
        else:
            reason = DEFAULT_REASON
            reason_defaulted = True
        extra_field_count = len(set(arguments) - ALLOWED_TOOL_ARGUMENT_KEYS)
        return SendEmailToolParseResult(
            tool_args=SendEmailToolArgs(
                recipient=recipient,
                reason=reason,
                reason_defaulted=reason_defaulted,
                extra_field_count=extra_field_count,
            )
        )

    @staticmethod
    def _build_subject(recipient: str) -> str:
        return f"Zgłoszenie skierowane do: {recipient}"

    @staticmethod
    def _build_mail_body(
        sender_email: str,
        recipient: str,
        original_message: str,
        reason: str,
        used_fallback: bool,
    ) -> str:
        # On a fallback the reason is the backend's description of the failed tool call,
        # not the model's justification, so it must not be labelled as coming from the AI.
        reason_label = "Przekierowanie awaryjne" if used_fallback else "Uzasadnienie (AI)"
        return "\n".join(
            [
                "Automatyczne skierowanie zgłoszenia przez agenta AI.",
                "",
                f"Nadawca: {sender_email}",
                f"Dział docelowy: {recipient}",
                "",
                "Treść zgłoszenia:",
                original_message,
                "",
                f"{reason_label}: {reason}",
            ]
        )
