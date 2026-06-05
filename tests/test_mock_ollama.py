import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from app.domain.departments import ALLOWED_RECIPIENTS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "mock_ollama.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_ollama", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mock_recipients_stay_subset_of_allowed() -> None:
    # The mock duplicates the department addresses (it runs standalone, with no app
    # package). This guards that the duplication never drifts outside the real set.
    module = load_module()
    recipients = {recipient for recipient, _keywords in module.KEYWORD_ROUTING}
    recipients.add(module.OTHER)
    assert recipients <= ALLOWED_RECIPIENTS


def test_mock_classifies_unambiguous_messages() -> None:
    module = load_module()

    assert module.classify("Nie działa mi komputer") == "help-desk@example.com"
    assert module.classify("Chciałbym zgłosić urlop na jutro") == "kadry@example.com"
    assert module.classify("Aplikacja zwraca błąd 500") == "it@example.com"
    assert module.classify("Mam pytanie o proces rekrutacji") == "human-resources@example.com"
    assert module.classify("Gdzie mogę zaparkować rower") == "other@example.com"


def test_mock_chat_response_uses_last_user_message_not_system() -> None:
    # The system prompt embeds every department's keywords; the mock must classify the
    # user turn only, so a help-desk user message is not shadowed by the system text.
    module = load_module()
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": "login password vacation recruitment komputer"},
            {"role": "user", "content": '{"message": "Nie działa mi komputer"}'},
        ]
    }

    response = module.build_chat_response(payload)
    tool_call = response["message"]["tool_calls"][0]

    assert tool_call["function"]["name"] == "send_email"
    assert tool_call["function"]["arguments"]["recipient"] == "help-desk@example.com"
    assert tool_call["function"]["arguments"]["reason"]


def test_mock_tags_response_advertises_configured_model() -> None:
    module = load_module()

    tags = module.build_tags_response()
    names = {entry["name"] for entry in tags["models"]}
    models = {entry["model"] for entry in tags["models"]}

    # ollama-init greps for "<model>" and the API's /ready check reads name/model.
    assert module.MODEL in names
    assert module.MODEL in models


def test_mock_chat_response_defaults_to_other_for_empty_payload() -> None:
    module = load_module()

    response = module.build_chat_response({})
    recipient = response["message"]["tool_calls"][0]["function"]["arguments"]["recipient"]

    assert recipient == "other@example.com"
