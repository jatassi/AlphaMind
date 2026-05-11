# 09 — E2E verify + runbook + bootstrap integration

## Goal

Ship the operator-facing verification + runbook surface for State persistence and integrate it into the central end-to-end verification runbook. After this story, an operator can run `uv run python scripts/verify_state_persistence.py` against a freshly-migrated DB, see a pass/fail summary covering migrations + invocation context + Phase 1 fill integration + Phase 2 envelope writeback (both accepted-envelope and Layer-1-parse-failure paths) + repository read parity, and consult `scripts/RUNBOOK_state_persistence.md` for prerequisites + failure-mode triage. The central `scripts/RUNBOOK_end_to_end_verification.md` also lists this layer in dependency-ordered position.

## Reading

* `scripts/verify_bootstrap.py` — the central bootstrap verification this story integrates with; pattern to mirror
* `scripts/verify_position_thesis_model.py` and `scripts/RUNBOOK_position_thesis_model.md` — the most recent sibling-feature verification surface; reuse argparse + reporting style
* `scripts/RUNBOOK_end_to_end_verification.md` — the central per-feature dependency-ordered runbook this story extends with a State persistence section
* `docs/design/05-execution-layer/state-persistence.md` § Snapshot isolation — the contract the verification exercises end-to-end
* Stories 02b through 08 — every contract this verification covers
* `src/alphamind/execution/oms/submit_envelope_mcp.py` § `SubmitEnvelopeState`, `failed_submission_log`, `FailedSubmissionEntry` — the in-memory surfaces story 08's writeback pairs with; verification covers both
* Parent issue [ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119) Notes for the orchestrator — reference for migration ordering invariants the verification asserts

## Depends on

* <issue id="ee4687a9-8a34-4008-9c59-bb163e043a0c">ALP-366</issue> (this work tree, story 08) — every contract this story exercises has landed.

## Scope

### 1\. `scripts/verify_state_persistence.py`

CLI shape:

```
uv run python scripts/verify_state_persistence.py [--db-path PATH] [--output FORMAT]
```

* `--db-path`: defaults to the configured `main.yaml` database path. Must be a freshly-migrated DB (the script does NOT migrate; it asserts the DB is at head revision and fails if not).
* `--output`: `text` (default) or `json`.

The script exercises six phases:

**Phase A — schema verification.** Inspects `sqlite_master` to confirm every State persistence table exists (`process_lifetimes`, `invocations`, `activity_log`, `positions`, `theses`, `thesis_components`, `orders`, `brackets`, `bracket_legs`, `cash_ledger`, `drawdown_state`, `fill_records`, `corporate_action_integration_ledger`). Reports missing tables.

**Phase B — InvocationContext round-trip.** Constructs a synthetic `ProcessLifetimeRecord` + `InvocationRecord`, opens an `InvocationContext`, exits cleanly, asserts both rows persist. Then opens a second context that raises a synthetic exception inside, asserts no rows persist. Asserts atomicity.

**Phase C — Phase 1 write path.** Constructs a fixture order + position via direct INSERT, appends an unprocessed fill via `append_fill_record`, runs `process_unprocessed_fills(handle)`, asserts the fill transitioned to `processed`, the position transitioned PENDING → OPEN, the activity log carries `order_filled` + `position_opened` + `bracket_activated` + `cash_debited` entries.

**Phase D — Phase 2 write path (accepted envelope).** Constructs a synthetic `PMEnvelope` with one OPEN command, runs `persist_envelope_outcome`, asserts the new position + thesis + bracket + orders persist, asserts the activity log carries the documented event chain plus a `pm_decision` entry.

**Phase E — Phase 2 write path (Layer-1 parse failure).** Constructs a malformed envelope payload (e.g., missing `verdict` field), invokes `_handle_submit_envelope` with an injected `InvocationHandle`, asserts both (a) the in-memory `state.failed_submission_log` got one new `FailedSubmissionEntry` AND (b) the `activity_log` table got one new `envelope_parse_failed` entry whose detail carries `raw_args_json` + `validation_error_repr`. Confirms the parallel-surface contract from story 08.

**Phase F — Repository read parity.** Constructs a `SqlPortfolioStateRepository` against the populated DB, runs `assemble_snapshot(repo, ...)`, asserts the produced `PortfolioStateSnapshot` reflects everything Phases B–E persisted. Asserts `RepositoryConsistencyError` fires when invoked before Phase 1 commits.

The script returns exit code 0 on full success, non-zero with a per-phase summary on any failure. Output format matches `verify_position_thesis_model.py`'s style.

### 2\. `scripts/RUNBOOK_state_persistence.md`

A markdown runbook with sections:

* **Prerequisites** — DB at head revision, `state_persistence:` section in `main.yaml` populated, environment variables documented.
* **Verification command** — the `verify_state_persistence.py` invocation with explanations.
* **Expected output shape** — annotated example output showing each phase's pass line.
* **Failure-mode triage table** — a table mapping each phase's typical failure (missing migration, FK violation, transaction rollback misbehavior, repository consistency error, snapshot category mismatch, Layer-1 parse-failure SQL mismatch) to its diagnostic command and likely fix.
* **Operational caveats** — note on the `agent_calls`, `saved_queries`, and feedback-loop entities being deferred per parent issue Pre-resolved decision (B); note on the `command_type` extension on `CommandAbandonedDetail` (story 02a) being load-bearing for the strategist projection; note that `submission_log.json` and `failed_submission_log.json` (the PM diagnostic archive artifacts from <issue id="5f68a494-e2db-48ea-b3d2-c89bc278cc21">ALP-353</issue>) coexist with the SQL `activity_log` persistence — the file artifacts are operator-facing forensics, the SQL entries are queryable analytics.

Mirror the structure of `scripts/RUNBOOK_position_thesis_model.md`.

### 3\. Update `scripts/RUNBOOK_end_to_end_verification.md`

Insert a "State persistence" phase in dependency-ordered position — after the data-layer + risk-guardrails phases that produce its inputs, before the decision-layer-pipeline-composition phase that consumes its outputs. The phase entry includes:

* The verification command (`uv run python scripts/verify_state_persistence.py`).
* What pass-state confirms (durable substrate live; downstream pipeline composition can be invoked).
* What downstream phase's inputs depend on this phase's pass-state.

### 4\. Bootstrap integration

Update `scripts/verify_bootstrap.py` (or its dependency-list) to include the new State persistence tables in the table-existence assertions. Touchpoint: the `_REQUIRED_TABLES` list (or equivalent) at the top of the script.

### Out of scope

* No new test surface — verification logic is a CLI script consuming the existing test surface.
* No `agent_calls`, `saved_queries`, feedback-loop entities (deferred per parent issue Pre-resolved decision (B)).
* No production data migration — the verification script runs against fresh DBs and ephemeral test fixtures.

## Acceptance criteria

- [ ] `scripts/verify_state_persistence.py` exists and accepts the documented CLI args.
- [ ] Running against a freshly-migrated DB produces a pass-line for each of Phases A–F.
- [ ] Running against a DB missing one of the State persistence tables fails Phase A with a clear "missing table: X" message and exits non-zero.
- [ ] Running against a DB at head with all tables present produces a 0 exit code and prints a six-section pass summary.
- [ ] Phase E asserts both the in-memory `failed_submission_log` and the SQL `envelope_parse_failed` entry are populated for a Layer-1 parse failure.
- [ ] `scripts/RUNBOOK_state_persistence.md` exists with all five sections (prerequisites, verification command, expected output, triage table, operational caveats) and the operational-caveats section names the `failed_submission_log.json` ↔ SQL `envelope_parse_failed` parallel-surface relationship.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` is updated with a State persistence phase in the right dependency-ordered position; the surrounding phases reference it correctly.
- [ ] `scripts/verify_bootstrap.py` includes the new State persistence tables in its existence assertions.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.
- [ ] Running `uv run python scripts/verify_state_persistence.py` against a fresh DB produces exit 0 and prints "ALL PHASES PASS".

## Verification

Run `uv run python scripts/verify_state_persistence.py --output text` against a freshly-migrated test DB and confirm exit 0 plus the six-phase pass summary. Read the runbook end-to-end (`cat scripts/RUNBOOK_state_persistence.md`) and confirm the failure-mode triage table is operator-actionable. Spot-check `scripts/RUNBOOK_end_to_end_verification.md` for the inserted State persistence section in correct dependency-ordered position.