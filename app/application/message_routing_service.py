from typing import Protocol

from app.agent.routing_agent import RouteResult
from app.schemas.messages import MessageRouteRequest, MessageRouteResponse


class SupportsRoute(Protocol):
    """Minimal contract the service needs from a routing agent."""

    def route(self, sender_email: str, message: str) -> RouteResult: ...


class MessageRoutingService:
    def __init__(self, routing_agent: SupportsRoute) -> None:
        self.routing_agent = routing_agent

    def route_message(
        self,
        request: MessageRouteRequest,
    ) -> MessageRouteResponse:
        result = self.routing_agent.route(
            sender_email=str(request.email),
            message=request.message,
        )

        return MessageRouteResponse(
            status=result.status,
            recipient=result.recipient,
            reply_to=result.reply_to,
            subject=result.subject,
            reason=result.reason,
            used_fallback=result.used_fallback,
            model=result.model,
            routing_confidence=result.routing_confidence,
        )
