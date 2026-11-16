import json
import re

from app.agent.issue_boundary import (
    ISSUE_SELECTION_PROMPT,
    MAX_SELECTION_INPUT_BYTES,
    build_issue_selection_request,
)
from app.agent.prompts import LANGUAGE_GUIDANCE, build_routing_messages
from app.domain.departments import DepartmentEmail, RoutingCategory
from app.domain.routing_policy import ROUTING_EXAMPLES

SELECTOR_GUIDANCE = (
    " The message is usually Polish, often written without Polish diacritics; read each "
    "word in its ordinary Polish meaning and do not translate it."
)


def test_selector_prompt_ends_with_generic_polish_guidance() -> None:
    assert ISSUE_SELECTION_PROMPT.endswith(SELECTOR_GUIDANCE)
    assert ISSUE_SELECTION_PROMPT.count("without Polish diacritics") == 1


def test_classifier_prompt_has_guidance_once_before_decision_process() -> None:
    system = build_routing_messages("", "")[0]["content"]
    assert system.count(LANGUAGE_GUIDANCE) == 1
    assert system.index(LANGUAGE_GUIDANCE) < system.index("Decision process:")
    assert "without Polish diacritics" in LANGUAGE_GUIDANCE
    assert "do not translate" in LANGUAGE_GUIDANCE


def test_language_guidance_names_no_category_recipient_or_example_word() -> None:
    # Content words only: short function words such as "do" exist in both languages.
    example_words = {
        word.lower()
        for message, _ in ROUTING_EXAMPLES
        for word in re.findall(r"\w+", message)
        if len(word) >= 4
    }
    for guidance in (LANGUAGE_GUIDANCE, SELECTOR_GUIDANCE):
        words = {word.lower() for word in re.findall(r"\w+", guidance) if len(word) >= 4}
        assert "@" not in guidance
        assert not words & {category.value for category in RoutingCategory}
        assert not any(email.value in guidance for email in DepartmentEmail)
        assert not words & example_words


def test_selector_budget_still_admits_a_1200_character_ascii_message() -> None:
    # One lexical unit keeps the grammar tiny, so only the input byte budget is measured.
    message = "a" * 1200
    messages, _ = build_issue_selection_request(message)
    encoded = json.dumps({"messages": messages}, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= MAX_SELECTION_INPUT_BYTES
