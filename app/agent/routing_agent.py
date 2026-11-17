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
from app.agent.tool_schema import build_propose_route_tool_schema
from app.domain.departments import CATEGORY_RECIPIENTS, DepartmentEmail, RoutingCategory
from app.infrastructure.ollama_client import OllamaChatError


class ReviewRequiredError(RuntimeError):
    pass


RETRY_INSTRUCTION = (
    "You must call the propose_route tool exactly once. Do not answer in plain text."
)
ALLOWED_TOOL_ARGUMENT_KEYS = frozenset({"category", "reason"})
logger = logging.getLogger(__name__)


class SupportsChatWithTools(Protocol):
    """Minimal contract the agent needs from an Ollama-style chat client."""

    model: str

    def chat_with_schema(
        self, messages: list[dict[str, Any]], schema: dict[str, Any]
    ) -> dict[str, Any]: ...

    def chat_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


class ProposalErrorCategory(StrEnum):
    INVALID_TOOL_CALL = "invalid_route_proposal_tool_call"
    MALFORMED_ARGUMENTS = "malformed_route_proposal_arguments"
    INVALID_FIELDS = "invalid_route_proposal_fields"
    INVALID_CATEGORY = "invalid_route_proposal_category"
    INVALID_REASON = "invalid_route_proposal_reason"


@dataclass(frozen=True)
class RoutingProposal:
    recipient: str
    reason: str
    used_fallback: bool
    model: str
    routing_confidence: Literal["high", "low"]
    issue_quotes: list[str]


@dataclass(frozen=True)
class RouteProposalToolArgs:
    category: RoutingCategory
    reason: str


@dataclass(frozen=True)
class RouteProposalParseResult:
    tool_args: RouteProposalToolArgs | None
    error_category: ProposalErrorCategory | None = None


class RoutingAgent:
    def __init__(self, ollama_client: SupportsChatWithTools) -> None:
        self.ollama_client = ollama_client
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
        messages = build_routing_messages(
            sender_email=sender_email, message="\n".join(issue_quotes)
        )
        tools = [build_propose_route_tool_schema()]
        response = self.ollama_client.chat_with_tools(messages, tools)
        parse_result = self._parse_route_proposal_tool_calls(response)
        if parse_result.tool_args is None:
            retry_messages = [*messages, {"role": "system", "content": RETRY_INSTRUCTION}]
            response = self.ollama_client.chat_with_tools(retry_messages, tools)
            parse_result = self._parse_route_proposal_tool_calls(response)
        tool_args = parse_result.tool_args
        if tool_args is None:
            error = parse_result.error_category or ProposalErrorCategory.INVALID_TOOL_CALL
            # Never log untrusted keys, values, or the model's rejection content.
            logger.info("Routing proposal rejected reason_category=%s", error.value)
            raise OllamaChatError("Model did not provide a valid routing proposal.")
        recipient = CATEGORY_RECIPIENTS[tool_args.category].value
        logger.info(
            "Valid route proposal category=%s recipient=%s", tool_args.category.value, recipient
        )
        return RoutingProposal(
            recipient=recipient,
            reason=tool_args.reason,
            used_fallback=False,
            model=self.model,
            routing_confidence="low" if recipient == DepartmentEmail.OTHER.value else "high",
            issue_quotes=issue_quotes,
        )

    def route(self, sender_email: str, message: str) -> RoutingProposal:
        raise ReviewRequiredError("Human review is required before delivery.")

    def _parse_route_proposal_tool_calls(
        self, response: dict[str, Any]
    ) -> RouteProposalParseResult:
        calls = self._extract_tool_calls(response)
        if len(calls) != 1 or self._tool_call_name(calls[0]) != "propose_route":
            return RouteProposalParseResult(None, ProposalErrorCategory.INVALID_TOOL_CALL)
        arguments = self._parse_arguments(self._tool_call_arguments(calls[0]))
        if arguments is None:
            return RouteProposalParseResult(None, ProposalErrorCategory.MALFORMED_ARGUMENTS)
        return self._build_tool_args(arguments)

    @staticmethod
    def _extract_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
        # Native calls live inside message. Reject ambiguous or malformed call
        # collections instead of silently selecting one valid call.
        message = response.get("message")
        if not isinstance(message, dict) or response.get("tool_calls"):
            return []
        for metadata in (response, message):
            if "done" in metadata and metadata["done"] is not True:
                return []
            if metadata.get("done_reason") not in (None, "stop"):
                return []
            if metadata.get("finish_reason") not in (None, "stop"):
                return []
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
            return []
        return calls

    @staticmethod
    def _tool_call_name(tool_call: dict[str, Any]) -> str | None:
        function = tool_call.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return str(function["name"])
        return None

    @staticmethod
    def _tool_call_arguments(tool_call: dict[str, Any]) -> Any:
        function = tool_call.get("function")
        return function.get("arguments") if isinstance(function, dict) else None

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> dict[str, Any] | None:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            return None

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Duplicate proposal argument")
                value[key] = item
            return value

        try:
            parsed = json.loads(raw_arguments, object_pairs_hook=unique_object)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _build_tool_args(arguments: dict[str, Any]) -> RouteProposalParseResult:
        if set(arguments) != ALLOWED_TOOL_ARGUMENT_KEYS:
            return RouteProposalParseResult(None, ProposalErrorCategory.INVALID_FIELDS)
        category = arguments["category"]
        if not isinstance(category, str):
            return RouteProposalParseResult(None, ProposalErrorCategory.INVALID_CATEGORY)
        try:
            resolved_category = RoutingCategory(category)
        except ValueError:
            return RouteProposalParseResult(None, ProposalErrorCategory.INVALID_CATEGORY)
        reason = arguments["reason"]
        if not isinstance(reason, str) or not reason.strip():
            return RouteProposalParseResult(None, ProposalErrorCategory.INVALID_REASON)
        return RouteProposalParseResult(RouteProposalToolArgs(resolved_category, reason.strip()))
