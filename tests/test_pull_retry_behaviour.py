import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wait-for-ollama-and-pull-model.sh"


def _extract_pull_model(script_text: str) -> str:
    # Pull the real pull_model() definition out of the bootstrap script so the
    # test exercises the shipped code, not a reimplementation.
    lines = script_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("pull_model()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _build_harness() -> str:
    # Drive the extracted function with a stubbed curl and zero backoff. WANT_OK_ON
    # is the attempt number on which the fake curl starts succeeding.
    pull_model = _extract_pull_model(SCRIPT.read_text(encoding="utf-8"))
    return "\n".join(
        [
            "set -u",
            pull_model,
            "OLLAMA_MODEL=test-model",
            "OLLAMA_HOST=http://fake",
            "OLLAMA_PULL_MAX_ATTEMPTS=3",
            "OLLAMA_PULL_RETRY_DELAY_SECONDS=0",
            "calls=0",
            'curl() { calls=$((calls + 1)); [ "$calls" -ge "$WANT_OK_ON" ] '
            "&& return 0; return 7; }",
            "pull_model",
            'echo "rc=$? attempts=$calls"',
        ]
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="requires a POSIX sh")
@pytest.mark.parametrize(
    ("want_ok_on", "expected_rc", "expected_attempts"),
    [
        (99, 1, 3),  # every attempt fails -> retries exhausted, non-zero exit
        (3, 0, 3),  # fails twice then succeeds -> transient error, download saved
    ],
)
def test_pull_model_retries_until_success_or_exhausted(
    want_ok_on: int, expected_rc: int, expected_attempts: int
) -> None:
    result = subprocess.run(
        ["sh", "-c", _build_harness()],
        capture_output=True,
        text=True,
        env={**os.environ, "WANT_OK_ON": str(want_ok_on)},
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"rc={expected_rc} attempts={expected_attempts}" in result.stdout
