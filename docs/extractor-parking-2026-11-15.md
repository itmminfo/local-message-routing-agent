# Extractor checkpoint, parked on 15 November 2026

Work is paused at the owner's request. PR #7 remains a draft and is not merged.
No change to the main preview is part of this checkpoint. Local Mailpit and
explicit human approval remain mandatory.

## Retained implementation

- The literal selector prompt and bounded grammar builder are restored from
  `0c2e3e6b9ce0d3166ffc9545c16df648d6b4506f`. Limits remain 128 lexical units,
  3072 UTF-8 bytes of serialized selector messages and a 1 MiB schema.
- The original exact quote grounder and disjoint assignment implementation stay
  unchanged. The ordered wrapper preserves supplied source order and searches
  for the earliest legal lexical occurrence, skipping earlier inside-word hits.
- Incomplete model responses and unexpected tools fail closed. No invalid or
  empty selection is rescued with the full source.
- The classifier keeps native `propose_route(category, reason)` and the existing
  recipient allowlist. Its full historical policy, boundary rules and examples
  are restored using static category identifiers. No user keyword routing was
  added. `office_facilities` and `other` both map to the existing other recipient.
- The classifier's measured system payload is 7102 LF bytes, fingerprint
  `f741484eee78`. It passed an eleven-case development pilot, including the
  kitchen coffee machine, and two A/B/A contrasts. This is not full acceptance.

## Unresolved and rejected

The two HR messages that previously produced no selection remain unresolved.
No full frozen benchmark has been completed on this final parking checkpoint.
Do not infer model quality from green CI.

The full-partition/compact-classifier runtime `b6da9d7` fixed the three target
cases but introduced 23 new failures on the 145 already published development
inputs. It was rejected. Its second candidate run was interrupted and its active
preview rejected. The complete baseline runs and first candidate run, partial
second candidate and original raw checkpoints are retained separately.

Off-source mode selection, fixed-unit labeling, targeted semantic rules and
single-unit/role-only controls also failed their declared acceptance criteria.
Failures included selecting routing controls as evidence and losing real JSON
error context. Successful structural checks are not semantic model acceptance.
None of these experimental extractors is retained in the application.

The new 30-case holdout was authored and annotated by a separate data-only
agent. It was measured by the rejected runtime, but implementers have not read
its content and neither implementers nor result auditors inspected its results.
It remains withheld for a future candidate. Old datasets and evidence were not
rewritten. The 145 published inputs are development data.

## Validation and resumption

The parked code passes 448 pytest tests, 52 UI tests, Ruff, format checking,
mypy, Bandit and both Compose configuration checks. No model inference was
added after the parking request. Earlier synthetic Mailpit/restart evidence
belongs to its recorded runtime, not to this final unmeasured checkpoint.

The next step is an owner decision about whether to test another local model.
The proposed `qwen3:8b` was not downloaded or approved. The official
[Ollama model page](https://ollama.com/library/qwen3:8b) lists approximately
5.2 GB and tool support; that does not establish suitability for this task.

If work resumes, first run the full known 145-case development check. Freeze
the accepted code, model digest, profile and image before inspecting the new
holdout. Then complete both frozen candidate series with cold API/Ollama
restarts, independent quote/metric checks and a new approval/replay Mailpit E2E
on that exact image. Do not reuse rejected candidate outcomes as final results.
Do not merge, deploy or start those steps solely because a reminder fires.
