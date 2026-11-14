from typing import NoReturn, Protocol

from app.agent.routing_agent import ReviewRequiredError, RoutingProposal
from app.schemas.messages import MessageRouteRequest


class SupportsPropose(Protocol):
    def propose(self, sender_email: str, message: str) -> RoutingProposal: ...


class MessageRoutingService:
    """Legacy callers cannot send; the review service owns approved delivery."""

    def __init__(self, routing_agent: SupportsPropose) -> None:
        self.routing_agent = routing_agent

    def propose_message(self, request: MessageRouteRequest) -> RoutingProposal:
        return self.routing_agent.propose(str(request.email), request.message)

    def route_message(self, request: MessageRouteRequest) -> NoReturn:
        raise ReviewRequiredError("Human review is required before delivery.")
