# qwen3:8b evaluation on a local CPU host, 16-17 November 2026

Status: **not accepted (FAIL)**. PR #7 stays a draft. Nothing was merged and the main local preview was not changed.
Local Mailpit only and explicit human approval remain mandatory.

## Scope

The owner approved one new candidate model, `qwen3:8b`, and asked for all inference and tests to run on a separate
local CPU host instead of the workstation. The parked checkpoint `4317dac8255374600009e26440e7322cae459491` was
measured first on the 145 published development inputs, then two bounded candidates. The 30-case holdout stayed sealed:
no candidate passed the development contract, so there was no freeze and no 145+30 series.

## Environment

- Ollama 0.24.0, `qwen3:8b` digest `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41` (Q4_K_M, 8.2B,
  capabilities completion, tools, thinking). Effective context 4096, temperature 0.
- CPU only. The host hung hard during the first unrestricted inference (no kernel trace). All later runs limited the
  Ollama container to 8 CPUs (`cpus: 8`, `cpuset: 0-7`); this is a performance setting, not a quality setting.
- Isolated Compose project with its own Mailpit, review databases and loopback ports. Images were built from clean
  clones with unique tags and checked against the CI image content rule.
- Proposal-only driver: create, suggest, reject and read back. No approve, retry or SMTP path. Mailpit stayed at 0.

## Contract (written before any candidate result)

On the 145 development inputs, all at once: both HR cases (`HELD_OUT_CASES/04`, `CHALLENGE_CASES/10`) ready and correct;
no new ready-but-wrong suggestions and no correct decisions lost compared with the checkpoint run; no proposal for any of
the six instruction-only inputs; 100% valid quotes (literal, word boundaries, unique, ordered, disjoint); strict correct
decisions not below the checkpoint; complete series with the measurement safety invariants.

## Results on the 145 development inputs

| Run | Profile | Strict correct (139) | No proposal | Ready but wrong | Instruction-only with proposal | Median per case |
|---|---|---:|---:|---:|---:|---:|
| `4317dac` | thinking (default), `num_predict` 512 | 106 | 32 | 1 | 1/6 | 176 s |
| `4a9a8e4` | `OLLAMA_THINK=false`, 512 | 133 | 0 | 6 | 5/6 | 71 s |
| `3c2dddf` | thinking, 2048, language hint | 116 of 124 measured | 4 | 4 | not measured | 209 s |

- Checkpoint: all 32 missing proposals ended with `done_reason=length`; thinking used the whole 512-token budget.
- `think=false` removes the truncation but the selector stops separating instructions from the request (5/6
  instruction-only inputs got a proposal) and three previously correct cases became wrong. Contract points 1-4 fail.
- Thinking with 2048 tokens and a generic Polish-language hint fixes most truncations but introduces new wrong
  suggestions (PIT-11 reasoned as "not an employment document", a desk power button reasoned as "not a computing
  device"). The series stopped at 125/145 because the review session expired after 8 hours
  (`REVIEW_SESSION_TTL_SECONDS=28800`); it is informational only.
- All ready proposals in all runs had valid quotes. No run sent mail.

## HR04 is a model vocabulary limit

In every mode the selector returned the whole message verbatim, so extraction and grounding were correct. The classifier
read "wakat" (job vacancy) as "wakacje" (holiday) with thinking, without thinking and with the language hint. The only
pipeline-side fixes would be a keyword rule or a case exception, which the project rules forbid. Remaining options need an
owner decision: a different model with better Polish vocabulary (full new measurement), or a general HR policy
description that names job vacancies and candidate referrals (risk of fitting the known set).

## Measurement notes for the next attempt

- Series longer than about 7 hours need an evaluation profile with `REVIEW_SESSION_TTL_SECONDS` above the series length,
  or a driver that renews the session. Product code does not need to change for this.
- With thinking and 2048 tokens the longest single call took 496 s of the 600 s timeout.
- Candidate commits are on branch `claude/qwen3-hr-20261116` (`4a9a8e4` optional `OLLAMA_THINK`, default unchanged;
  `3c2dddf` language hint). They are continuation material, not accepted code.
- A rehearsal of the full CI gate on the host (Python 3.12 with hashed lockfile: Ruff, format, mypy, 448 pytest,
  52 UI tests, pip-audit, Bandit, both Compose configs) and of a synthetic Mailpit approval, replay and restart E2E with
  the mocked model passed for `4317dac`. That proves the flow, not AI quality.
