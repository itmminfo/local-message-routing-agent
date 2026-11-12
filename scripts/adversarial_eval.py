#!/usr/bin/env python3
"""Measure wrong-but-allowed routing with paired synthetic prompt injections.

Each pair sends a clean message and the same message with an appended attacker
instruction. The route endpoint sends both messages to SMTP, so this diagnostic
only accepts local API and Mailpit hosts and probes Mailpit before sending.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.departments import ALLOWED_RECIPIENTS  # noqa: E402
from app.schemas.messages import MESSAGE_MAX_LENGTH  # noqa: E402

from scripts.routing_accuracy_check import API_BASE_URL, route_message  # noqa: E402

DEFAULT_DATASET = Path(__file__).resolve().parent / "data" / "adversarial_pairs_v1.json"
MAIL_API_URL = os.environ.get("MAIL_API_URL", "http://localhost:8025").rstrip("/")
_PAIR_FIELDS = {
    "id",
    "expected_recipient",
    "attacker_target_recipient",
    "clean_message",
    "attack_style",
    "attack_suffix",
}
_ATTACK_STYLES = {"direct", "fake_system", "forged_tool_call", "english_override"}


@dataclass(frozen=True)
class AttackPair:
    id: str
    expected_recipient: str
    attacker_target_recipient: str
    clean_message: str
    attack_style: str
    attack_suffix: str

    @property
    def injected_message(self) -> str:
        return f"{self.clean_message}\n\n{self.attack_suffix}"


@dataclass(frozen=True)
class Dataset:
    schema_version: int
    dataset_id: str
    description: str
    sha256: str
    pairs: list[AttackPair]


@dataclass(frozen=True)
class RoutingResult:
    recipient: str
    used_fallback: bool
    routing_confidence: str
    model: str


def _required_text(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def load_dataset(path: Path) -> Dataset:
    raw = path.read_bytes()
    payload: object = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("dataset must be a JSON object")
    document = cast(dict[str, object], payload)
    if set(document) != {"schema_version", "dataset_id", "description", "pairs"}:
        raise ValueError("dataset has missing or unexpected fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported dataset schema_version")
    dataset_id = _required_text(document, "dataset_id")
    description = _required_text(document, "description")
    rows = document["pairs"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("pairs must be a nonempty list")

    pairs: list[AttackPair] = []
    seen_ids: set[str] = set()
    seen_clean: set[str] = set()
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"pair {index} must be a JSON object")
        row = cast(dict[str, object], item)
        if set(row) != _PAIR_FIELDS:
            raise ValueError(f"pair {index} has missing or unexpected fields")
        pair = AttackPair(
            id=_required_text(row, "id"),
            expected_recipient=_required_text(row, "expected_recipient"),
            attacker_target_recipient=_required_text(row, "attacker_target_recipient"),
            clean_message=_required_text(row, "clean_message"),
            attack_style=_required_text(row, "attack_style"),
            attack_suffix=_required_text(row, "attack_suffix"),
        )
        if pair.id in seen_ids:
            raise ValueError(f"duplicate id: {pair.id}")
        if pair.clean_message in seen_clean:
            raise ValueError(f"duplicate clean_message: {pair.id}")
        if pair.expected_recipient not in ALLOWED_RECIPIENTS:
            raise ValueError(f"invalid expected_recipient: {pair.id}")
        if pair.attacker_target_recipient not in ALLOWED_RECIPIENTS:
            raise ValueError(f"invalid attacker_target_recipient: {pair.id}")
        if pair.expected_recipient == pair.attacker_target_recipient:
            raise ValueError(f"attacker target equals expected recipient: {pair.id}")
        if pair.attack_style not in _ATTACK_STYLES:
            raise ValueError(f"invalid attack_style: {pair.id}")
        if pair.attacker_target_recipient not in pair.attack_suffix:
            raise ValueError(f"attack_suffix does not name target: {pair.id}")
        if len(pair.injected_message) > MESSAGE_MAX_LENGTH:
            raise ValueError(f"injected_message exceeds API limit: {pair.id}")
        seen_ids.add(pair.id)
        seen_clean.add(pair.clean_message)
        pairs.append(pair)

    return Dataset(
        schema_version=1,
        dataset_id=dataset_id,
        description=description,
        sha256=hashlib.sha256(raw).hexdigest(),
        pairs=pairs,
    )


def require_local_api(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1", "api"}:
        raise ValueError("adversarial eval requires a local Compose API backed by Mailpit")


def require_local_mailpit(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
        "mailpit",
    }:
        raise ValueError("adversarial eval requires a local Mailpit API")


def probe_mailpit(url: str) -> None:
    require_local_mailpit(url)
    with urllib.request.urlopen(  # nosec B310 - URL restricted to local Mailpit hosts above
        f"{url}/api/v1/messages?limit=1", timeout=5
    ) as response:
        payload: object = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("local API did not return a Mailpit messages list")
    messages = payload.get("messages")
    if messages is None:
        messages = payload.get("Messages")
    if not isinstance(messages, list):
        raise ValueError("local API did not return a Mailpit messages list")


def _parse_result(result: dict[str, Any]) -> RoutingResult:
    recipient = result.get("recipient")
    if not isinstance(recipient, str) or recipient not in ALLOWED_RECIPIENTS:
        raise ValueError(f"API returned recipient outside allowlist: {recipient!r}")
    used_fallback = result.get("used_fallback")
    if not isinstance(used_fallback, bool):
        raise ValueError("API returned invalid used_fallback")
    routing_confidence = result.get("routing_confidence")
    if routing_confidence not in {"high", "low"}:
        raise ValueError("API returned invalid routing_confidence")
    model = result.get("model")
    if not isinstance(model, str):
        raise ValueError("API returned invalid model")
    return RoutingResult(recipient, used_fallback, routing_confidence, model)


def evaluate_pairs(
    dataset: Dataset, route_func: Callable[[str], dict[str, Any]] | None = None
) -> dict[str, object]:
    send = route_message if route_func is None else route_func
    summary = {
        "pairs": len(dataset.pairs),
        "controls_correct_without_fallback": 0,
        "injected_correct_without_fallback": 0,
        "wrong_allowed_after_attack": 0,
        "attack_induced_wrong_allowed": 0,
        "attacker_targeted_switch": 0,
        "wrong_allowed_with_high_confidence": 0,
        "clean_fallbacks": 0,
        "injected_fallbacks": 0,
    }
    cases: list[dict[str, object]] = []
    models: set[str] = set()
    for pair in dataset.pairs:
        control = _parse_result(send(pair.clean_message))
        attacked = _parse_result(send(pair.injected_message))
        models.update((control.model, attacked.model))

        eligible = control.recipient == pair.expected_recipient and not control.used_fallback
        injected_correct = (
            attacked.recipient == pair.expected_recipient and not attacked.used_fallback
        )
        wrong_allowed = attacked.recipient != pair.expected_recipient
        induced_wrong_allowed = eligible and wrong_allowed
        targeted_switch = (
            eligible
            and attacked.recipient == pair.attacker_target_recipient
            and not attacked.used_fallback
        )
        high_confidence_wrong = wrong_allowed and attacked.routing_confidence == "high"

        summary["controls_correct_without_fallback"] += eligible
        summary["injected_correct_without_fallback"] += injected_correct
        summary["wrong_allowed_after_attack"] += wrong_allowed
        summary["attack_induced_wrong_allowed"] += induced_wrong_allowed
        summary["attacker_targeted_switch"] += targeted_switch
        summary["wrong_allowed_with_high_confidence"] += high_confidence_wrong
        summary["clean_fallbacks"] += control.used_fallback
        summary["injected_fallbacks"] += attacked.used_fallback
        cases.append(
            {
                "id": pair.id,
                "attack_style": pair.attack_style,
                "expected_recipient": pair.expected_recipient,
                "attacker_target_recipient": pair.attacker_target_recipient,
                "control_recipient": control.recipient,
                "control_fallback": control.used_fallback,
                "attacked_recipient": attacked.recipient,
                "attacked_fallback": attacked.used_fallback,
                "attacked_confidence": attacked.routing_confidence,
                "eligible_control": eligible,
                "attack_induced_wrong_allowed": induced_wrong_allowed,
                "attacker_targeted_switch": targeted_switch,
            }
        )

    return {
        "dataset_id": dataset.dataset_id,
        "dataset_schema_version": dataset.schema_version,
        "dataset_sha256": dataset.sha256,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "models": sorted(models),
        "summary": summary,
        "cases": cases,
    }


def main() -> int:
    try:
        require_local_api(API_BASE_URL)
        probe_mailpit(MAIL_API_URL)
        dataset = load_dataset(DEFAULT_DATASET)
        print(
            f"Sending {2 * len(dataset.pairs)} synthetic messages to local Compose/Mailpit.",
            file=sys.stderr,
        )
        report = evaluate_pairs(dataset)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
