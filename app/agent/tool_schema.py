from typing import Any

from app.domain.departments import RoutingCategory


def build_propose_route_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "propose_route",
            "description": "Propose a topic for human review. This tool never sends mail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [category.value for category in RoutingCategory],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["category", "reason"],
                "additionalProperties": False,
            },
        },
    }
