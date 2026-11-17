from enum import StrEnum


class DepartmentEmail(StrEnum):
    HUMAN_RESOURCES = "human-resources@example.com"
    HELP_DESK = "help-desk@example.com"
    IT = "it@example.com"
    KADRY = "kadry@example.com"
    OTHER = "other@example.com"


ALLOWED_RECIPIENTS = frozenset(member.value for member in DepartmentEmail)


class RoutingCategory(StrEnum):
    PEOPLE_PROCESS = "people_process"
    EMPLOYMENT_ADMINISTRATION = "employment_administration"
    COMPUTING_HARDWARE = "computing_hardware"
    SOFTWARE_ACCESS = "software_access"
    OFFICE_FACILITIES = "office_facilities"
    OTHER = "other"


# Only the backend resolves model-selected topics to human-review recipients.
CATEGORY_RECIPIENTS: dict[RoutingCategory, DepartmentEmail] = {
    RoutingCategory.PEOPLE_PROCESS: DepartmentEmail.HUMAN_RESOURCES,
    RoutingCategory.EMPLOYMENT_ADMINISTRATION: DepartmentEmail.KADRY,
    RoutingCategory.COMPUTING_HARDWARE: DepartmentEmail.HELP_DESK,
    RoutingCategory.SOFTWARE_ACCESS: DepartmentEmail.IT,
    RoutingCategory.OFFICE_FACILITIES: DepartmentEmail.OTHER,
    RoutingCategory.OTHER: DepartmentEmail.OTHER,
}

CATEGORY_DESCRIPTIONS: dict[RoutingCategory, str] = {
    RoutingCategory.PEOPLE_PROCESS: (
        "recruitment, candidates, interviews, onboarding as a people process, benefits, "
        "employee programs, training, development, feedback, career path, culture and "
        "employee relations."
    ),
    RoutingCategory.EMPLOYMENT_ADMINISTRATION: (
        "formal employment matters, vacation, sick leave, absence, payroll, salary, "
        "employment contracts and annexes, employee records and documents, working time, "
        "overtime, attendance, PIT and ZUS. This formal category takes precedence over "
        "generic people topics."
    ),
    RoutingCategory.COMPUTING_HARDWARE: (
        "faults of physical computers and computing peripherals only, such as laptops, "
        "monitors, keyboards, mice, printers and scanners; local computing workstation "
        "problems, printing and basic local software setup. Other physical objects and "
        "equipment are outside this category."
    ),
    RoutingCategory.SOFTWARE_ACCESS: (
        "accounts, login, passwords, permissions, platform access, applications, software "
        "errors, APIs, integrations, servers, databases and system-level network "
        "infrastructure. When both a computing device and software/access are mentioned, "
        "choose the dominant actionable topic."
    ),
    RoutingCategory.OFFICE_FACILITIES: (
        "office or building administration, maintenance, facilities, reception, logistics, "
        "non-IT purchasing, non-computing devices and workplace amenities. This is a "
        "positive category, including short and clear requests."
    ),
    RoutingCategory.OTHER: (
        "finance, vendor invoices, legal, students, any remaining unsupported topic, "
        "unclear topic, or unrelated issues that are equally important with no dominant topic."
    ),
}


def validate_department_email(value: str) -> DepartmentEmail:
    try:
        return DepartmentEmail(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(ALLOWED_RECIPIENTS))
        raise ValueError(
            f"Unknown department recipient: {value!r}. Allowed recipients are: {allowed}."
        ) from exc
