from typing import Any

from app.domain.departments import DepartmentEmail


def build_send_email_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "Send the user's message to the selected internal department email address."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "enum": [department.value for department in DepartmentEmail],
                        "description": (
                            "Department email address selected from the allowed recipients."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One short sentence in Polish justifying the chosen recipient."
                        ),
                    },
                },
                # The tool contract exposes only the fields the model may decide.
                # Subject, Reply-To, the body template, and final recipient validation
                # are handled by the application. Unexpected keys are ignored by
                # RoutingAgent and logged by name.
                "required": ["recipient", "reason"],
                "additionalProperties": False,
            },
        },
    }
