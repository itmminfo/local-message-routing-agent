import pytest
from app.agent.routing_agent import ReviewRequiredError, RoutingProposal
from app.application.message_routing_service import MessageRoutingService
from app.schemas.messages import MessageRouteRequest


class ForbiddenAgent:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, sender_email: str, message: str) -> RoutingProposal:
        self.calls += 1
        raise AssertionError("Legacy service called model or sender")


def test_legacy_python_service_cannot_bypass_review() -> None:
    agent = ForbiddenAgent()
    service = MessageRoutingService(routing_agent=agent)
    with pytest.raises(ReviewRequiredError, match="review|approval|Review"):
        service.route_message(MessageRouteRequest(email="jan.nowak@example.com", message="Issue"))
    assert agent.calls == 0
