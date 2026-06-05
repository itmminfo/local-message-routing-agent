from enum import StrEnum


class DepartmentEmail(StrEnum):
    HUMAN_RESOURCES = "human-resources@example.com"
    HELP_DESK = "help-desk@example.com"
    IT = "it@example.com"
    KADRY = "kadry@example.com"
    OTHER = "other@example.com"


ALLOWED_RECIPIENTS = frozenset(member.value for member in DepartmentEmail)


def validate_department_email(value: str) -> DepartmentEmail:
    try:
        return DepartmentEmail(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(ALLOWED_RECIPIENTS))
        raise ValueError(
            f"Unknown department recipient: {value!r}. Allowed recipients are: {allowed}."
        ) from exc
