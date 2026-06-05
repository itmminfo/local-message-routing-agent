import pytest
from app.domain.departments import (
    ALLOWED_RECIPIENTS,
    DepartmentEmail,
    validate_department_email,
)
from app.domain.routing_policy import (
    format_boundary_rules_for_prompt,
    format_routing_examples_for_prompt,
    format_routing_policy_for_prompt,
)

EXPECTED_RECIPIENTS = frozenset(
    {
        "human-resources@example.com",
        "help-desk@example.com",
        "it@example.com",
        "kadry@example.com",
        "other@example.com",
    }
)


def test_department_email_enum_has_exact_allowed_recipients() -> None:
    assert frozenset(member.value for member in DepartmentEmail) == EXPECTED_RECIPIENTS
    assert ALLOWED_RECIPIENTS == EXPECTED_RECIPIENTS


def test_all_allowed_recipients_validate() -> None:
    for recipient in EXPECTED_RECIPIENTS:
        assert validate_department_email(recipient).value == recipient


def test_unknown_recipient_fails_with_clear_message() -> None:
    with pytest.raises(ValueError, match="Unknown department recipient"):
        validate_department_email("unknown@example.com")


def test_routing_policy_includes_all_allowed_recipients() -> None:
    policy = format_routing_policy_for_prompt()

    for recipient in EXPECTED_RECIPIENTS:
        assert recipient in policy


def test_routing_policy_includes_route_when_and_do_not_route_when_sections() -> None:
    policy = format_routing_policy_for_prompt()

    assert "Route when:" in policy
    assert "Do not route when:" in policy
    assert "Human Resources / People team" in policy
    assert "Personnel administration / Payroll" in policy
    assert "IT systems / Applications / Access" in policy


def test_boundary_rules_include_hr_kadry_and_help_desk_it_boundaries() -> None:
    boundary_rules = format_boundary_rules_for_prompt()

    assert "Employee administration beats generic HR" in boundary_rules
    assert "kadry@example.com" in boundary_rules
    assert "Recruitment and people-development topics go to HR" in boundary_rules
    assert "human-resources@example.com" in boundary_rules
    assert "Physical user equipment goes to help-desk" in boundary_rules
    assert "help-desk@example.com" in boundary_rules
    assert "Accounts, access and applications go to IT" in boundary_rules
    assert "it@example.com" in boundary_rules


def test_routing_examples_include_examples_for_all_recipients() -> None:
    examples = format_routing_examples_for_prompt()

    for recipient in EXPECTED_RECIPIENTS:
        assert recipient in examples
    assert '"Chciałbym zgłosić urlop na jutro" -> kadry@example.com' in examples
    assert '"Mam pytanie o proces rekrutacji" -> human-resources@example.com' in examples
    assert '"Nie działa mi komputer" -> help-desk@example.com' in examples
    assert '"Nie mogę zalogować się do platformy szkoleniowej" -> it@example.com' in examples
    assert '"Mam pytanie dotyczące faktury od dostawcy" -> other@example.com' in examples
