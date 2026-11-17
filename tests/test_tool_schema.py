from app.agent.tool_schema import build_propose_route_tool_schema
from app.domain.departments import CATEGORY_RECIPIENTS, RoutingCategory


def test_native_proposal_tool_has_exact_semantic_contract() -> None:
    tool = build_propose_route_tool_schema()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "propose_route"
    assert (
        tool["function"]["description"]
        == "Propose a topic for human review. This tool never sends mail."
    )
    parameters = tool["function"]["parameters"]
    assert parameters["type"] == "object"
    assert set(parameters["properties"]) == {"category", "reason"}
    assert parameters["required"] == ["category", "reason"]
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["category"] == {
        "type": "string",
        "enum": [category.value for category in RoutingCategory],
    }
    assert parameters["properties"]["reason"] == {"type": "string"}


def test_tool_exposes_no_delivery_fields_or_addresses() -> None:
    tool = build_propose_route_tool_schema()
    assert "@" not in str(tool)
    assert set(tool["function"]["parameters"]["properties"]).isdisjoint(
        {"recipient", "to", "reply_to", "sender_email", "subject", "body"}
    )
    assert set(CATEGORY_RECIPIENTS) == set(RoutingCategory)
