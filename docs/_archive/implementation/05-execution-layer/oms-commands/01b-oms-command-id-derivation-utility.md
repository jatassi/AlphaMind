# 01b — OMS command-ID derivation utility

## Goal

Land the canonical command-ID derivation utility at `src/alphamind/execution/oms/command_ids.py`. Two derivation functions (PM-originated and engine-originated), one helper to count post-rejection modifications on an envelope, and inverse parsers for round-trip extraction. Per parent decision (F), this work tree owns the helper module since `oms-command-ids.md` is part of the OMS-commands design surface. Story 03 swaps the engine-stub's inline `_format_command_id` for `derive_pm_command_id`; the continuous monitor work tree consumes `derive_engine_command_id` when emitting envelopes.

## Reading

* `docs/design/oms-command-ids.md` § PM-originated command IDs — format, components, uniqueness rules; the `attempt_seq` computation rule (count of `phase == "post_rejection"` modification entries)
* `docs/design/oms-command-ids.md` § Engine-originated command IDs — `MON.{monitor_session_id}.{trigger_id}.{command_ordinal}` format and the no-`attempt_seq` rule
* `docs/design/oms-command-ids.md` § Worked example — concrete IDs for `inv-2026-04-23T14-30Z.ENV-REC-2.0.0` then `.0.1` after rejection
* `docs/design/04-decision-layer/pm-envelope-schema.md` § `modification_record` — the `phase: "pre_submission" | "post_rejection"` partition that drives `attempt_seq`
* `src/alphamind/execution/oms/submit_envelope_mcp.py` — the existing inline `_format_command_id` (line ~882) and `_attempt_seq` (line ~724) helpers; story 01b extracts them as canonical primitives. Note the `inv-` prefix gate ("prefixes inv- if the supplied invocation_id does not already start with it").
* `src/alphamind/decision/portfolio_manager/models.py` — `PMEnvelope.modifications: tuple[ModificationRecord, ...]` shape that `compute_attempt_seq` reads

## Depends on

(No same-tree blockers — Wave 1.)

## Scope

In scope, all under `src/alphamind/execution/oms/`. Tests at `tests/execution/oms/`.

### 1\. `command_ids.py`

Author the module exporting:

* `derive_pm_command_id(*, invocation_id: str, envelope_id: str, command_ordinal: int, attempt_seq: int) -> str` — formats `inv-{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}`. Prefixes `inv-` only if `invocation_id` does not already start with it (mirrors the existing helper's gating). Raises `ValueError` with a recognizable message for: `command_ordinal < 0`, `attempt_seq < 0`, `envelope_id` not matching `^ENV-(REC|SA|SA-ORD)-[0-9]+$`. Returns a string matching the regex `^inv-[^.]+\.ENV-(REC|SA|SA-ORD)-[0-9]+\.[0-9]+\.[0-9]+$`.
* `derive_engine_command_id(*, monitor_session_id: str, trigger_id: int, command_ordinal: int = 0) -> str` — formats `MON.{monitor_session_id}.{trigger_id}.{command_ordinal}`. Default `command_ordinal=0` per design (cascades produce multiple envelopes, not multiple commands). Raises `ValueError` for: `monitor_session_id` empty or containing `.`, `trigger_id < 0`, `command_ordinal < 0`. Returns a string matching `^MON\.[^.]+\.[0-9]+\.[0-9]+$`.
* `compute_attempt_seq(envelope: PMEnvelope) -> int` — counts entries in `envelope.modifications` whose `phase == "post_rejection"`. The first submission has `attempt_seq=0`; each guardrail-rejection-driven modification increments. Imports `PMEnvelope` from `alphamind.decision.portfolio_manager` (existing module).
* `parse_pm_command_id(command_id: str) -> PMCommandIdComponents` — inverse of `derive_pm_command_id`. Returns a frozen Pydantic `PMCommandIdComponents(invocation_id, envelope_id, command_ordinal, attempt_seq)`. Raises `ValueError` for malformed IDs.
* `parse_engine_command_id(command_id: str) -> EngineCommandIdComponents` — inverse of `derive_engine_command_id`. Returns a frozen `EngineCommandIdComponents(monitor_session_id, trigger_id, command_ordinal)`. Raises `ValueError` for malformed IDs.
* `is_pm_originated(command_id: str) -> bool` and `is_engine_originated(command_id: str) -> bool` — boolean discriminators that classify a command_id by prefix without raising.

### 2\. `__init__.py`

Append `command_ids` re-exports (`derive_pm_command_id`, `derive_engine_command_id`, `compute_attempt_seq`, `parse_pm_command_id`, `parse_engine_command_id`, `is_pm_originated`, `is_engine_originated`, `PMCommandIdComponents`, `EngineCommandIdComponents`) to the public surface.

### 3\. Tests at `tests/execution/oms/test_command_ids.py`

* Round-trip: each derive function's output parses cleanly via the matching parse function.
* `derive_pm_command_id` matches the worked example in `oms-command-ids.md` § Worked example (`inv-2026-04-23T14-30Z.ENV-REC-2.0.0` and `.0.1`).
* `derive_pm_command_id` prefixes `inv-` only when missing (covers both branches of the prefix gate).
* `derive_engine_command_id` produces `MON.<session>.0.0` for default `command_ordinal`.
* Negative cases: empty / dotted `monitor_session_id`; negative `command_ordinal`; negative `attempt_seq`; non-matching `envelope_id` pattern; non-matching `command_id` strings against `parse_*` functions.
* `compute_attempt_seq`: zero post-rejection entries → 0; one → 1; mixed pre + post → counts only post; reads from a real `PMEnvelope` fixture (synthesize via Pydantic, not file-based).
* `is_pm_originated` / `is_engine_originated` cover both classes and reject non-OMS strings.

### Out of scope

* Duplicate-detection state — that's a per-invocation/session concern handled by the engine-stub upgrade (story 03) and the engine envelope submission path (story 04). The utility is stateless.
* Schema validation of envelopes — story 01a's models handle that.
* Wiring this utility into the engine-stub — story 03.

## Acceptance criteria

- [ ] `src/alphamind/execution/oms/command_ids.py` exists and exports `derive_pm_command_id`, `derive_engine_command_id`, `compute_attempt_seq`, `parse_pm_command_id`, `parse_engine_command_id`, `is_pm_originated`, `is_engine_originated`, `PMCommandIdComponents`, `EngineCommandIdComponents`.
- [ ] `derive_pm_command_id(invocation_id="inv-2026-04-23T14-30Z", envelope_id="ENV-REC-2", command_ordinal=0, attempt_seq=0)` returns `"inv-2026-04-23T14-30Z.ENV-REC-2.0.0"`.
- [ ] `derive_pm_command_id(invocation_id="2026-04-23T14-30Z", ...)` (no `inv-` prefix) returns the same string with `inv-` prepended.
- [ ] `derive_engine_command_id(monitor_session_id="abc-123", trigger_id=42)` returns `"MON.abc-123.42.0"`.
- [ ] `parse_pm_command_id("inv-2026-04-23T14-30Z.ENV-REC-2.0.1")` returns `PMCommandIdComponents(invocation_id="2026-04-23T14-30Z", envelope_id="ENV-REC-2", command_ordinal=0, attempt_seq=1)`.
- [ ] `parse_engine_command_id("MON.abc-123.42.0")` returns `EngineCommandIdComponents(monitor_session_id="abc-123", trigger_id=42, command_ordinal=0)`.
- [ ] `compute_attempt_seq` returns `len([m for m in envelope.modifications if m.phase == "post_rejection"])` for any `PMEnvelope`.
- [ ] `is_pm_originated("inv-…")` is `True`; `is_engine_originated("MON.…")` is `True`; both reject malformed strings (return `False`, do not raise).
- [ ] Negative cases for derive functions raise `ValueError` with message naming the offending field.
- [ ] `tests/execution/oms/test_command_ids.py` covers every acceptance criterion above and passes under `uv run pytest tests/execution/oms/test_command_ids.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/execution/oms/test_command_ids.py -n auto` and confirm the worked example from `oms-command-ids.md` round-trips.
