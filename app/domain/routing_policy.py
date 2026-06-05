from typing import TypedDict

from app.domain.departments import DepartmentEmail


class DepartmentRule(TypedDict):
    label: str
    scope: str
    route_when: list[str]
    do_not_route_when: list[str]


DEPARTMENT_ROUTING_RULES: dict[DepartmentEmail, DepartmentRule] = {
    DepartmentEmail.HUMAN_RESOURCES: {
        "label": "Human Resources / People team",
        "scope": (
            "soft HR, recruitment, onboarding, benefits, development and people-related topics"
        ),
        "route_when": [
            "recruitment process, candidates, CV, interviews, application status",
            "onboarding as a people process",
            "benefits, medical care, sports card, employee programs",
            "training, development, feedback, career path",
            "workplace culture, employee relations, soft HR topics",
        ],
        "do_not_route_when": [
            "vacation, sick leave, payroll, employment contract, annex, "
            "employee documents, working time records, PIT, ZUS",
        ],
    },
    DepartmentEmail.KADRY: {
        "label": "Personnel administration / Payroll",
        "scope": (
            "hard HR administration, employee records, payroll and formal employment matters"
        ),
        "route_when": [
            "vacation, day off, leave request",
            "sick leave, absence, L4",
            "employment contract, annex, employee documents, certificate of employment",
            "salary, payroll, payslip, PIT, ZUS",
            "working time records, overtime, schedule, attendance",
            "formal employment administration",
        ],
        "do_not_route_when": [
            "recruitment, candidates, benefits, career development, employee culture",
        ],
    },
    DepartmentEmail.HELP_DESK: {
        "label": "User help desk / Workplace support",
        "scope": ("first-line user support, physical workstation, hardware and peripherals"),
        "route_when": [
            "computer, laptop, monitor, keyboard, mouse, printer, scanner",
            "hardware does not work or does not turn on",
            "local workstation problems",
            "basic user support",
            "basic local software setup on user's computer",
            "printing and peripheral issues",
        ],
        "do_not_route_when": [
            "login, account, permissions, platform access, system/application "
            "bug, API, integration, infrastructure",
        ],
    },
    DepartmentEmail.IT: {
        "label": "IT systems / Applications / Access",
        "scope": (
            "systems, applications, accounts, permissions, platform access, "
            "software errors and infrastructure"
        ),
        "route_when": [
            "login, password, account, permissions, access",
            "training platform, internal system, application, panel, CMS",
            "application error, system error, API, integration",
            "server, database, infrastructure, system-level network issue",
            "technical issue related to software/system rather than physical equipment",
        ],
        "do_not_route_when": [
            "physical computer, laptop, printer, monitor, keyboard, mouse, "
            "local workstation hardware",
        ],
    },
    DepartmentEmail.OTHER: {
        "label": "Other / Unclassified",
        "scope": "fallback for unclear, unrelated or unsupported topics",
        "route_when": [
            "message is too vague to infer a department",
            "message is unrelated to the listed departments",
            "message contains several unrelated issues with no dominant intent",
            "message asks for general contact without any concrete topic",
            "message concerns finance/vendor invoices/facilities/legal/students "
            "if not clearly covered above",
        ],
        "do_not_route_when": [
            "message is short but clear",
            "message is informal but clear",
            "message has typos but clear intent",
            "message does not use exact department names but clearly matches a department",
        ],
    },
}


BOUNDARY_RULES = [
    (
        "Employee administration beats generic HR: vacation, sick leave, payroll, "
        "contract, employee documents, working time -> kadry@example.com."
    ),
    (
        "Recruitment and people-development topics go to HR: recruitment, "
        "candidates, onboarding, benefits, development, feedback, culture -> "
        "human-resources@example.com."
    ),
    (
        "Physical user equipment goes to help-desk: computer, laptop, monitor, "
        "printer, scanner, keyboard, mouse, local workstation -> help-desk@example.com."
    ),
    (
        "Accounts, access and applications go to IT: login, password, permissions, "
        "platform, system, application, CMS, panel, API, integration -> it@example.com."
    ),
    (
        "If a message contains both a device issue and an application/access issue, "
        "choose the dominant actionable issue. If the dominant issue is "
        "login/access/platform/application, choose it@example.com. If the dominant "
        "issue is physical equipment, choose help-desk@example.com."
    ),
    (
        "Use other@example.com only when no listed department clearly matches or "
        "when several unrelated issues are equally important."
    ),
    (
        "Do not route to other@example.com merely because the message is short, "
        "informal, or contains typos."
    ),
]


ROUTING_EXAMPLES = [
    ("Chciałbym zgłosić urlop na jutro", DepartmentEmail.KADRY),
    ("Potrzebuję zaświadczenia o zatrudnieniu", DepartmentEmail.KADRY),
    ("Nie zgadza mi się wynagrodzenie", DepartmentEmail.KADRY),
    ("Mam pytanie o proces rekrutacji", DepartmentEmail.HUMAN_RESOURCES),
    (
        "Chciałbym zapytać o benefity pracownicze",
        DepartmentEmail.HUMAN_RESOURCES,
    ),
    ("Nie działa mi komputer", DepartmentEmail.HELP_DESK),
    ("Drukarka nie drukuje", DepartmentEmail.HELP_DESK),
    ("Laptop nie chce się uruchomić", DepartmentEmail.HELP_DESK),
    ("Nie mogę zalogować się do platformy szkoleniowej", DepartmentEmail.IT),
    ("Potrzebuję dostępu do systemu", DepartmentEmail.IT),
    ("Aplikacja zwraca błąd", DepartmentEmail.IT),
    ("Mam sprawę organizacyjną", DepartmentEmail.OTHER),
    ("Proszę o kontakt w ważnej sprawie", DepartmentEmail.OTHER),
    ("Mam pytanie dotyczące faktury od dostawcy", DepartmentEmail.OTHER),
]


def _append_bullets(lines: list[str], title: str, items: list[str]) -> None:
    lines.append(f"  {title}:")
    for item in items:
        lines.append(f"    - {item}")


def format_routing_policy_for_prompt() -> str:
    lines = ["Allowed routing recipients:"]
    for department, rules in DEPARTMENT_ROUTING_RULES.items():
        lines.append(f"- {department.value} ({rules['label']})")
        lines.append(f"  Scope: {rules['scope']}")
        _append_bullets(lines, "Route when", rules["route_when"])
        _append_bullets(lines, "Do not route when", rules["do_not_route_when"])
    return "\n".join(lines)


def format_boundary_rules_for_prompt() -> str:
    lines = ["Boundary rules:"]
    for rule in BOUNDARY_RULES:
        lines.append(f"- {rule}")
    return "\n".join(lines)


def format_routing_examples_for_prompt() -> str:
    lines = ["Routing examples:"]
    for message, department in ROUTING_EXAMPLES:
        lines.append(f'"{message}" -> {department.value}')
    return "\n".join(lines)
