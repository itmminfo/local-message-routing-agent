import asyncio

import httpx
import pytest
from app.api.routes import get_message_routing_service
from app.main import create_app
from app.schemas.messages import MessageRouteResponse


class UnapprovedDeliveryTrap:
    def __init__(self) -> None:
        self.calls = 0

    def route_message(self, request: object) -> MessageRouteResponse:
        self.calls += 1
        return MessageRouteResponse(
            status="sent",
            recipient="help-desk@example.com",
            reply_to="sender@example.com",
            subject="Unapproved delivery",
            reason="Unapproved delivery",
            used_fallback=False,
            model="test-model",
            routing_confidence="high",
        )


@pytest.mark.parametrize("path", ["messages/route", "messages/route-local-eval"])
def test_legacy_route_cannot_deliver_without_review(path: str) -> None:
    target = create_app()
    trap = UnapprovedDeliveryTrap()
    target.dependency_overrides[get_message_routing_service] = lambda: trap

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target), base_url="http://localhost"
        ) as client:
            return await client.post(
                f"/api/v1/{path}",
                headers={"Origin": "http://localhost"},
                json={"email": "sender@example.com", "message": "Nie działa komputer"},
            )

    response = asyncio.run(call())
    assert (response.status_code, trap.calls) == (409, 0)
