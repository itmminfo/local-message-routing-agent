from app.agent.routing_agent import RouteResult
from app.application.message_routing_service import MessageRoutingService
from app.schemas.messages import MessageRouteRequest


class FakeRoutingAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def route(self, sender_email: str, message: str) -> RouteResult:
        self.calls.append({"sender_email": sender_email, "message": message})
        return RouteResult(
            status="sent",
            recipient="help-desk@example.com",
            reply_to=sender_email,
            subject="Zgłoszenie skierowane do: help-desk@example.com",
            reason="The message concerns a computer issue.",
            used_fallback=False,
            model="qwen2.5:3b",
            routing_confidence="high",
        )


def test_message_routing_service_delegates_to_routing_agent() -> None:
    agent = FakeRoutingAgent()
    service = MessageRoutingService(routing_agent=agent)

    service.route_message(
        MessageRouteRequest(
            email="jan.nowak@example.com",
            message="Nie działa mi komputer",
        )
    )

    assert agent.calls == [
        {
            "sender_email": "jan.nowak@example.com",
            "message": "Nie działa mi komputer",
        }
    ]


def test_message_routing_service_maps_route_result_to_response() -> None:
    service = MessageRoutingService(routing_agent=FakeRoutingAgent())

    response = service.route_message(
        MessageRouteRequest(
            email="jan.nowak@example.com",
            message="Nie działa mi komputer",
        )
    )

    assert response.status == "sent"
    assert response.recipient == "help-desk@example.com"
    assert response.reply_to == "jan.nowak@example.com"
    assert response.subject == "Zgłoszenie skierowane do: help-desk@example.com"
    assert response.reason == "The message concerns a computer issue."
    assert response.used_fallback is False
    assert response.model == "qwen2.5:3b"
    assert response.routing_confidence == "high"
