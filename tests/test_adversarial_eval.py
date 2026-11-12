from __future__ import annotations

from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from app.domain.departments import ALLOWED_RECIPIENTS
from app.domain.routing_policy import ROUTING_EXAMPLES
from scripts import adversarial_eval
from scripts.routing_accuracy_check import CHALLENGE_CASES


def pair(
    pair_id: str, expected: str, target: str, clean: str, suffix: str = "Override"
) -> adversarial_eval.AttackPair:
    return adversarial_eval.AttackPair(
        id=pair_id,
        expected_recipient=expected,
        attacker_target_recipient=target,
        clean_message=clean,
        attack_style="direct",
        attack_suffix=suffix,
    )


def response(recipient: str, *, fallback: bool = False, confidence: str = "high") -> dict[str, Any]:
    return {
        "recipient": recipient,
        "used_fallback": fallback,
        "routing_confidence": confidence,
        "model": "test-model",
    }


def test_v1_dataset_is_balanced_new_and_fixed() -> None:
    dataset = adversarial_eval.load_dataset(adversarial_eval.DEFAULT_DATASET)
    assert dataset.schema_version == 1
    assert dataset.dataset_id == "synthetic-adversarial-pairs-v1"
    assert len(dataset.pairs) == 20
    assert len(dataset.sha256) == 64

    assert {(p.expected_recipient, p.attacker_target_recipient) for p in dataset.pairs} == {
        (source, target)
        for source in ALLOWED_RECIPIENTS
        for target in ALLOWED_RECIPIENTS
        if source != target
    }
    assert Counter(p.attack_style for p in dataset.pairs) == {
        "direct": 5,
        "fake_system": 5,
        "forged_tool_call": 5,
        "english_override": 5,
    }
    assert len({p.id for p in dataset.pairs}) == 20
    assert len({p.clean_message for p in dataset.pairs}) == 20
    old_messages = {message for _category, message, _expected in CHALLENGE_CASES}
    old_messages.update(message for message, _recipient in ROUTING_EXAMPLES)
    assert not old_messages.intersection(p.clean_message for p in dataset.pairs)
    assert all(
        p.injected_message == f"{p.clean_message}\n\n{p.attack_suffix}"
        and p.attacker_target_recipient in p.attack_suffix
        for p in dataset.pairs
    )


def test_dataset_hash_is_the_same_for_lf_and_crlf(tmp_path: Path) -> None:
    raw = adversarial_eval.DEFAULT_DATASET.read_bytes().replace(bytes([13, 10]), bytes([10]))
    path = tmp_path / "pairs.json"
    path.write_bytes(raw)
    lf_hash = adversarial_eval.load_dataset(path).sha256
    path.write_bytes(raw.replace(bytes([10]), bytes([13, 10])))
    assert adversarial_eval.load_dataset(path).sha256 == lf_hash


def test_metrics_require_correct_control_before_counting_paired_switch() -> None:
    pairs = [
        pair("targeted", "kadry@example.com", "human-resources@example.com", "clean A"),
        pair("wrong-control", "it@example.com", "help-desk@example.com", "clean B"),
        pair("fallback", "human-resources@example.com", "other@example.com", "clean C"),
        pair("other-control-fallback", "other@example.com", "it@example.com", "clean D"),
    ]
    answers = {
        "clean A": response("kadry@example.com"),
        pairs[0].injected_message: response("human-resources@example.com"),
        "clean B": response("kadry@example.com"),
        pairs[1].injected_message: response("help-desk@example.com"),
        "clean C": response("human-resources@example.com"),
        pairs[2].injected_message: response("other@example.com", fallback=True, confidence="low"),
        "clean D": response("other@example.com", fallback=True, confidence="low"),
        pairs[3].injected_message: response("it@example.com"),
    }
    dataset = adversarial_eval.Dataset(
        schema_version=1,
        dataset_id="test",
        description="test",
        sha256="test",
        pairs=pairs,
    )

    report = adversarial_eval.evaluate_pairs(dataset, route_func=answers.__getitem__)
    assert report["summary"] == {
        "pairs": 4,
        "controls_correct_without_fallback": 2,
        "injected_correct_without_fallback": 0,
        "wrong_allowed_after_attack": 4,
        "paired_wrong_allowed_switch": 2,
        "paired_targeted_switch": 1,
        "wrong_allowed_with_high_confidence": 3,
        "clean_fallbacks": 1,
        "injected_fallbacks": 1,
    }
    assert report["cases"][1]["eligible_control"] is False
    assert report["cases"][3]["eligible_control"] is False


def test_evaluator_rejects_non_allowlisted_api_result() -> None:
    candidate = pair("x", "kadry@example.com", "it@example.com", "clean")
    dataset = adversarial_eval.Dataset(1, "test", "test", "test", [candidate])

    with pytest.raises(ValueError, match="recipient outside allowlist"):
        adversarial_eval.evaluate_pairs(
            dataset, route_func=lambda _message: response("attacker@example.com")
        )


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8025", "http://127.0.0.1:8025", "http://mailpit:8025"],
)
def test_mailpit_guard_accepts_local_targets(url: str) -> None:
    adversarial_eval.require_local_mailpit(url)


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.10:8025"])
def test_mailpit_guard_rejects_remote_targets(url: str) -> None:
    with pytest.raises(ValueError, match="local Mailpit"):
        adversarial_eval.require_local_mailpit(url)


def test_mailpit_probe_requires_messages_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adversarial_eval.urllib.request,
        "urlopen",
        lambda _url, timeout: BytesIO(b'{"messages":[]}'),
    )
    adversarial_eval.probe_mailpit("http://localhost:8025")

    monkeypatch.setattr(
        adversarial_eval.urllib.request,
        "urlopen",
        lambda _url, timeout: BytesIO(b'{"not_mailpit":true}'),
    )
    with pytest.raises(ValueError, match="Mailpit messages list"):
        adversarial_eval.probe_mailpit("http://localhost:8025")


def test_compose_guard_rejects_external_smtp_even_with_local_mailpit() -> None:
    adversarial_eval.require_safe_compose(
        "http://api:8000", "http://mailpit:8025", "mailpit", "1025"
    )
    with pytest.raises(ValueError, match="SMTP_HOST=mailpit"):
        adversarial_eval.require_safe_compose(
            "http://api:8000", "http://mailpit:8025", "smtp.example.com", "587"
        )
    with pytest.raises(ValueError, match="Compose tools"):
        adversarial_eval.require_safe_compose(
            "http://localhost:8000", "http://mailpit:8025", "mailpit", "1025"
        )


def test_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "pairs.json"
    path.write_text(
        '{"schema_version":1,"dataset_id":"test","description":"test","pairs":['
        '{"id":"same","expected_recipient":"kadry@example.com",'
        '"attacker_target_recipient":"it@example.com","clean_message":"A",'
        '"attack_style":"direct","attack_suffix":"it@example.com"},'
        '{"id":"same","expected_recipient":"it@example.com",'
        '"attacker_target_recipient":"kadry@example.com","clean_message":"B",'
        '"attack_style":"direct","attack_suffix":"kadry@example.com"}]}'
    )
    with pytest.raises(ValueError, match="duplicate id"):
        adversarial_eval.load_dataset(path)
