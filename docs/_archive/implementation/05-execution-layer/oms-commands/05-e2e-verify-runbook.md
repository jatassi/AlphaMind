# 05 — E2E verify + runbook

## Goal

Ship the operator-facing verification + runbook surface for OMS commands and integrate it into the central end-to-end verification runbook. After this story, an operator can run `uv run python scripts/verify_oms_commands.py` against a freshly-migrated DB, see a pass/fail summary covering canonical Pydantic round-trip + PM envelope path (Layer-1 → Layer-2/3 → guardrail re-validate → Phase 2 writeback → activity log) + engine envelope path (Layer-1 → submit_engine_envelope → Phase 2 close writeback → activity log with engine_guardrail provenance), and read `scripts/RUNBOOK_oms_commands.md` for triage steps. The central runbook gains an OMS commands phase positioned after State persistence verification and before Decision-layer composition.

## Reading

* `scripts/verify_state_persistence.py` and `scripts/RUNBOOK_state_persistence.md` — sibling pattern for verify scripts that exercise Phase 2 write paths against a real DB
* `scripts/verify_pm.py` and `scripts/RUNBOOK_pm.md` — sibling pattern for live-SDK verify with scenario fixtures
* `scripts/RUNBOOK_end_to_end_verification.md` — the central runbook; insert OMS commands phase between State persistence and Decision-layer composition
* `src/alphamind/execution/oms/command_models.py` (story 01a) — canonical models the verify script constructs
* `src/alphamind/execution/oms/submit_envelope_mcp.py` (post-story-03) — engine-stub the PM envelope verify path exercises
* `src/alphamind/execution/oms/submit_engine_envelope.py` (story 04) — function the engine envelope verify path exercises
* `src/alphamind/execution/state_persistence/repository/sql_repository.py` — `SqlPortfolioStateRepository` for read-back assertions in the verify script
* Parent issue <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> § Notes for the orchestrator — architectural invariants the verify script asserts

## Depends on

* <issue id="beb08c17-ddd8-4217-a2bd-611ade38f150">ALP-374</issue> (03) — engine-stub upgrade with faithful Phase 2 writeback (PM envelope verify path)
* <issue id="1da9391d-a106-40ef-9a1b-b83b5bb69150">ALP-375</issue> (04) — engine envelope submission path (engine envelope verify path)

## Scope

### 1\. `src/alphamind/scripts/verify_oms_commands.py` (testable module)

Author the testable verify module covering five phases:

* **Phase 1 — Canonical model round-trip.** Construct one `OpenCommand`, one `CloseCommand` (each `close_rationale_type` variant), one `AdjustCommand` (each change-field variant), one `CancelCommand`, one `AddCommand` via Pydantic. Round-trip each via `TypeAdapter[OMSCommand]`. Assert no `ValidationError`. Construct one valid `EngineEnvelope` and round-trip via `TypeAdapter[EngineEnvelope]`.
* **Phase 2 — Command-ID utility.** Call `derive_pm_command_id` and `derive_engine_command_id` against the worked example from `oms-command-ids.md`. Round-trip via `parse_pm_command_id` / `parse_engine_command_id`. Compute `attempt_seq` from a synthesized `PMEnvelope` with mixed pre + post modifications.
* **Phase 3 — PM envelope path against a fresh DB.** Set up an `InvocationContext`, build an MCP server via `build_submit_envelope_mcp_server`, call the tool with a hand-constructed canonical PMEnvelope carrying one OPEN command. Assert: `SubmissionResult.status == "accepted"`; `command_id` matches PM-originated regex; `SqlPortfolioStateRepository.get_open_positions()` includes the new position with **real** `dollar_value` (not stub); the bracket has the right number of legs (matching `command.invalidation_legs[]`); the thesis has the right number of components; the activity log includes the expected entries (`order_submitted`, `thesis_created`, `capital_reserved`, `pm_decision`).
* **Phase 4 — Engine envelope path against the same DB.** Construct a valid `EngineEnvelope` carrying one CLOSE on an existing position (the one Phase 3 opened). Call `submit_engine_envelope`. Assert: `SubmissionResult.status == "accepted"`; `command_id` matches engine-originated regex; the position transitions to closed (or quantity reduces if partial); the bracket dissolves; the activity log includes a CLOSE entry with `engine_guardrail` provenance and the trigger record's `position_selection_rationale` preserved.
* **Phase 5 — Acceptance criterion summary.** Print one `[PASS]` or `[FAIL]` line per acceptance criterion with the name and the asserted condition. Exit zero on all-pass; exit non-zero with a diagnostic summary on any failure. Mirror the pattern from `verify_state_persistence.py`.

### 2\. `scripts/verify_oms_commands.py` (CLI shim)

Three-line shim that defers to `src.alphamind.scripts.verify_oms_commands.main()`. Mirrors the existing pattern in `scripts/verify_pm.py`.

### 3\. `scripts/RUNBOOK_oms_commands.md`

Author the operator runbook covering:

* **Prerequisites** — fresh DB migration applied; production DB path resolved (use the `/Volumes/...` path on macOS dev per CLAUDE.md, or the default Windows path on the production server). The verify script does NOT call any vendor API — it exercises Pydantic + SQL + the engine-stub end-to-end.
* **Invocation** — `uv run python scripts/verify_oms_commands.py` (no flags). Expected runtime under 1 minute (no live SDK calls).
* **Expected output shape** — one line per acceptance criterion with `[PASS]` / `[FAIL]` markers; final summary `RESULT: PASS (N/N criteria)` or `RESULT: FAIL (M/N criteria, see above)`.
* **Failure-mode triage table** — for each likely failure mode: the symptom (which line in the script's output), the probable cause, and the first diagnostic step. Examples: "command_id regex mismatch" → check `derive_pm_command_id` argument shape; "position dollar_value asserts $1k" → story 03 incomplete (stub constants still in writeback); "engine envelope rejection on accepted" → check `secondary_breach_check_result` in the test envelope.

### 4\. Insert into `scripts/RUNBOOK_end_to_end_verification.md`

Add the OMS commands phase to the dependency-ordered phase list. Place it after the State persistence phase and before any Decision-layer composition phase. The phase block describes what the verify exercises, the invocation, expected runtime, and links to `RUNBOOK_oms_commands.md` for triage.

### 5\. Tests at `tests/scripts/test_verify_oms_commands.py`

Cover that `verify_oms_commands.main()` returns 0 on a clean DB. Cover that introducing a deliberate failure (e.g., monkey-patching `_writeback_open` to retain a stub constant) produces a non-zero exit and surfaces the failing criterion. Mock the live DB layer if needed; the existing pattern in `tests/scripts/` covers similar cases.

### Out of scope

* Live SDK invocation (no Anthropic API calls in this verify — that's PM's verify_pm.py)
* Live broker calls (broker routing is deferred per parent decision (C))
* Continuous monitor process spin-up (the engine envelope test constructs the envelope directly)

## Acceptance criteria

- [ ] `src/alphamind/scripts/verify_oms_commands.py` exists and exports `main() -> int` that prints per-criterion `[PASS]`/`[FAIL]` lines and exits zero on success.
- [ ] `scripts/verify_oms_commands.py` exists as a 3-line shim deferring to the testable module.
- [ ] `scripts/RUNBOOK_oms_commands.md` exists and covers Prerequisites, Invocation, Expected output shape, and a Failure-mode triage table.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` includes a new OMS commands phase block positioned between State persistence and Decision-layer composition.
- [ ] The verify script's Phase 1 (canonical model round-trip) covers all five command variants (OPEN / CLOSE / ADJUST / CANCEL / ADD) plus engine envelope.
- [ ] The verify script's Phase 2 (command-ID utility) covers PM derivation + engine derivation + round-trip parse + attempt_seq computation.
- [ ] The verify script's Phase 3 (PM envelope path) constructs a canonical PMEnvelope with OPEN, calls `submit_envelope` MCP, and asserts the persisted position carries **real** dollar_value (not $1k stub).
- [ ] The verify script's Phase 4 (engine envelope path) constructs an `EngineEnvelope` with CLOSE, calls `submit_engine_envelope`, and asserts the activity log entry carries `engine_guardrail` provenance and the trigger record's `position_selection_rationale`.
- [ ] `uv run python scripts/verify_oms_commands.py` exits zero against a freshly-migrated DB and prints a `RESULT: PASS` summary line.
- [ ] `tests/scripts/test_verify_oms_commands.py` covers main()-returns-0 happy path and main()-returns-nonzero failure detection.
- [ ] `uv run pytest tests/scripts/test_verify_oms_commands.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

Run `uv run python scripts/verify_oms_commands.py` against a freshly-migrated DB; confirm it exits zero and prints the expected per-criterion summary. Spot-check the central runbook's phase ordering — OMS commands sits after State persistence and before any Decision-layer composition.
