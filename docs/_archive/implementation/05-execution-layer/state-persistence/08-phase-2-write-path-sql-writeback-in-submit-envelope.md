# 08 — Phase 2 write path: SQL writeback in submit_envelope

## Goal

Extend the engine-stub `submit_envelope_mcp.py` wrapper with a SQL writeback path: every accepted command (OPEN / CLOSE / ADJUST / CANCEL / ADD) produces the corresponding state mutations + activity-log entries inside a transaction; Layer-1 envelope parse failures (the <issue id="5f68a494-e2db-48ea-b3d2-c89bc278cc21">ALP-353</issue> `failed_submission_log` path) emit an `envelope_parse_failed` activity-log entry. Each accepted envelope's `pm_decision` event carries the full envelope detail (envelope_id, source provenance, evaluation, modifications array, command IDs) per parent issue Pre-resolved decision (C). After this story, both PM-accepted commands AND PM-attempted-but-Layer-1-rejected envelopes persist as durable state, and the activity log captures the full PM-attempt lineage. The PM stub's in-memory `SubmitEnvelopeState` cell remains for cumulative-state tracking inside the tool-use loop, but committed outcomes also write through to SQL.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Phase 2 write path — the per-command-type integration sequences for OPEN, CLOSE, ADJUST, CANCEL, ADD; the `command_abandoned` policy on submission failure
* `src/alphamind/execution/oms/submit_envelope_mcp.py` — the existing PM-stub wrapper this story extends. Read end-to-end: `_handle_submit_envelope`, `_process_commands`, `_process_one_command`, `_build_acknowledgment`, `_build_rejection_payload`, `SubmitEnvelopeState`, `FailedSubmissionEntry`, `failed_submission_log`. The Layer-1 path (`_validate_envelope_payload` → `ValidationError`) was added in <issue id="5f68a494-e2db-48ea-b3d2-c89bc278cc21">ALP-353</issue> (commit b555fd9742e3a95edcea9a51fe65abf7319571af) and now appends a `FailedSubmissionEntry` to `state.failed_submission_log` before returning the envelope-level rejection.
* `src/alphamind/decision/portfolio_manager/harness.py` § `_DiagState._archive_submission_logs` — writes both `submission_log.json` and `failed_submission_log.json` (per <issue id="5f68a494-e2db-48ea-b3d2-c89bc278cc21">ALP-353</issue>) to the diagnostic archive. This story does NOT replace those file artifacts; it adds a parallel SQL persistence path.
* `src/alphamind/decision/portfolio_manager/__init__.py` — `OMSCommand` discriminated union (`OpenCommand`, `CloseCommand`, `AdjustCommand`, `CancelCommand`, `AddCommand`), `PMEnvelope` shape
* `src/alphamind/portfolio_state/events/activity_log.py` § `OrderSubmittedDetail`, `OrderCancelledDetail`, `OrderModifiedDetail`, `BracketDissolvedDetail`, `BracketModifiedDetail`, `ThesisCreatedDetail`, `ThesisComponentAddedDetail`, `ThesisComponentUpdatedDetail`, `ThesisResolvedDetail`, `CapitalReservedDetail`, `CapitalReleasedDetail`, `PMDecisionDetail`, `CommandAbandonedDetail` — the activity-log details Phase 2 emits. This story ALSO adds a new `EnvelopeParseFailedDetail` to this file plus a new `EventType.ENVELOPE_PARSE_FAILED` value.
* Story 02a (`CommandAbandonedDetail.command_type`) — the typed-record extension this story consumes
* Story 03 (`append_activity_log_entry`) — the emission helper
* Story 02b (`InvocationContext`) — the transaction substrate
* Stories 04a–04d — the Tier 1 tables this story mutates

## Depends on

* <issue id="ee98d475-a31f-4bcd-af6f-c87c4779e1ec">ALP-365</issue> (this work tree, story 07) — Phase 1 write path is the sibling discipline; both share the activity-log emission helper and the InvocationContext substrate.
* <issue id="a2d5027a-e949-472c-9f91-aed3216e126c">ALP-355</issue> (this work tree, story 02a) — `CommandAbandonedDetail.command_type` field is required for the per-command-type framing on the `command_abandoned` event detail.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/write_paths/` plus a coordinated edit to `src/alphamind/execution/oms/submit_envelope_mcp.py` plus a typed-record extension at `src/alphamind/portfolio_state/events/activity_log.py`. Tests at `tests/execution/state_persistence/` and updates to `tests/execution/oms/`.

### 1\. Typed-record extension: `EnvelopeParseFailedDetail`

In `src/alphamind/portfolio_state/events/activity_log.py`:

* Add `ENVELOPE_PARSE_FAILED = "envelope_parse_failed"` to the `EventType` StrEnum.
* Add `EnvelopeParseFailedDetail(BaseModel)` Pydantic class with fields: `attempted_envelope_id: str`, `attempted_command_id: str`, `validation_error_repr: str`, `raw_args_json: str` (the dict-serialised raw args from `FailedSubmissionEntry.raw_args`).
* Wire the new variant into the discriminated-union dispatch.

### 2\. Phase 2 entry point

In `src/alphamind/execution/state_persistence/write_paths/phase2.py`:

```python
async def persist_envelope_outcome(
    handle: InvocationHandle,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist an accepted envelope's per-command outcomes inside the open transaction."""

async def persist_envelope_parse_failure(
    handle: InvocationHandle,
    failed_entry: FailedSubmissionEntry,
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist a Layer-1 parse failure as an envelope_parse_failed activity log entry."""
```

For each accepted command, dispatch on the discriminated union to apply the per-command-type integration:

* **OPEN** — INSERT new position (status PENDING), thesis (with components), bracket (status PENDING_ENTRY), entry order (status PENDING) + protective leg orders (OTO status). Reserve capital in cash_ledger. Emit `order_submitted`, `thesis_created`, `capital_reserved`.
* **CLOSE** — INSERT close order (status PENDING). Mark remaining bracket legs for cancellation on fill. Emit `order_submitted`.
* **ADJUST** — INSERT new protective leg orders, mark old protective orders CANCELLED, update bracket modification history, update thesis components if changed. Emit `order_cancelled` + `order_submitted` + `bracket_modified` + (optionally) `thesis_component_updated`.
* **CANCEL** — Mark target order CANCELLED. If entry leg: cancel all bracket legs, resolve thesis CANCELLED, mark bracket DISSOLVED. Release reserved capital. Emit `order_cancelled` + `capital_released` + (entry case) `thesis_resolved` + `bracket_dissolved`.
* **ADD** — INSERT add-entry order (status PENDING). Append new thesis component. Reserve capital. Optionally cancel + resubmit modified protective legs. Emit `order_submitted` + `thesis_component_added` + `capital_reserved` + (optionally) bracket modification entries.

### 3\. Single envelope-level `pm_decision` emission

After per-command writes, emit ONE `pm_decision` activity log entry per accepted envelope. Detail carries the full `PMDecisionDetail` (envelope_id, source provenance, evaluation, modifications array, resulting command IDs). Per parent issue Pre-resolved decision (C), this single emission is the durable record of envelope-level decisions; no separate `envelopes` table.

### 4\. `envelope_parse_failed` emission policy (Layer-1 failures)

When `_validate_envelope_payload` raises `ValidationError` (Layer-1 Pydantic failure):

* The existing in-memory append to `state.failed_submission_log` happens unchanged (preserves <issue id="5f68a494-e2db-48ea-b3d2-c89bc278cc21">ALP-353</issue> behavior).
* If an `InvocationHandle` is present, ALSO call `persist_envelope_parse_failure(handle, failed_entry)` to emit the new `envelope_parse_failed` activity log entry. Detail's `raw_args_json` carries the JSON-serialised raw args; `validation_error_repr` is a copy of the in-memory entry's value.
* The emission happens in the same transaction as Phase 2's other writes. If no Phase 2 writes have started yet (typical for envelope-level rejections), the emission is a standalone single-entry write.

### 5\. `command_abandoned` emission policy (post-acceptance submission failures)

When the broker submission window exhausts without success per the design doc:

* Roll back the per-command transaction.
* Emit `command_abandoned` in a post-rollback transaction (the contract: this entry survives the rollback of the main command transaction).
* Detail carries `envelope_id`, `command_id`, `originating_agent`, `failure_reason`, `retry_attempt_count`, plus the `command_type` field from story 02a.

### 6\. Wire into `submit_envelope_mcp.py`

Coordinated edit to `src/alphamind/execution/oms/submit_envelope_mcp.py`:

* `_handle_submit_envelope` accepts an optional `InvocationHandle` parameter; when present:
  * On Layer-1 parse failure (the existing `except ValidationError` block): call `persist_envelope_parse_failure(handle, FailedSubmissionEntry(...))` BEFORE returning the envelope-level rejection.
  * On accepted envelope: call `persist_envelope_outcome(handle, envelope, results)` AFTER the in-memory cumulative-state cell update.
* When `InvocationHandle` is None (legacy fixture-only callers), behavior is unchanged. Keeps `tests/execution/oms/test_submit_envelope_mcp.py` passing without rewriting its in-memory test surface.
* Add the new code path to `build_submit_envelope_mcp_server` so production composition pipelines (<issue id="5e187e5c-03a3-4d2b-9b33-d94c44c793af">ALP-310</issue>) can inject the handle.

### 7\. Atomicity contract

Each accepted envelope's per-command writes commit as a single transaction (all OPEN-time entities — position, thesis, bracket, orders — succeed or none do). The `envelope_parse_failed` emission for a Layer-1 failure is a single-entry transaction. Cross-command writes within an envelope can be sequenced.

### 8\. Activity_log CHECK constraint extension

Ship a small Alembic migration extending the `activity_log.event_type` CHECK constraint to include the new `"envelope_parse_failed"` value. `down_revision` chains to whichever migration last extended the constraint (story 03's initial migration, or a 04\*/05 migration if any of those added an event type — verify during integration).

### 9\. Tests

Tests at `tests/execution/state_persistence/test_phase2_write_path.py` cover:

* OPEN happy path: position + thesis + bracket + entry order + protective orders persist; `order_submitted` + `thesis_created` + `capital_reserved` + `pm_decision` activity log entries written.
* CLOSE happy path: close order persists; `order_submitted` + `pm_decision` written.
* ADJUST happy path: old orders → CANCELLED, new orders → PENDING, bracket modification history appended, `bracket_modified` + `pm_decision` written.
* CANCEL entry case: bracket → DISSOLVED, thesis → CANCELLED, capital released, full event chain written.
* ADD happy path: add-entry order + thesis component + capital reservation persist.
* `command_abandoned` post-rollback emission: synthetic submission failure aborts the per-command transaction but `command_abandoned` entry persists.
* `envelope_parse_failed` emission: a Layer-1 ValidationError inside `_handle_submit_envelope` (with InvocationHandle present) appends both an in-memory `FailedSubmissionEntry` AND an `envelope_parse_failed` activity log entry; the entry's detail carries `raw_args_json` + `validation_error_repr`.
* `envelope_parse_failed` without handle: when `InvocationHandle` is absent, only the in-memory `failed_submission_log` is mutated; no SQL write attempted.
* Multi-command envelope: three commands across an envelope produce one `pm_decision` entry referencing all three command IDs.

Update tests at `tests/execution/oms/test_submit_envelope_mcp.py` to add at least two tests exercising the SQL writeback path with an injected `InvocationHandle`: one happy-path accepted envelope, one Layer-1 parse failure.

### Out of scope

* No actual Alpaca submission — `submit_envelope_mcp.py` is still an engine stub (real submission lands in [ALP-120](https://linear.app/alphamind-jatassi/issue/ALP-120) OMS Commands).
* No engine-originated envelopes (continuous monitor) — that's [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123).
* No PortfolioStateSnapshot consumption — Phase 2 writes happen after the snapshot read.
* No removal of the `failed_submission_log.json` archive artifact — that's a sibling diagnostic surface to the SQL persistence; both coexist.

## Acceptance criteria

- [ ] `EventType.ENVELOPE_PARSE_FAILED` and `EnvelopeParseFailedDetail` exist in `src/alphamind/portfolio_state/events/activity_log.py` with all four named fields.
- [ ] `persist_envelope_outcome(handle, envelope, results)` exists with the documented signature.
- [ ] `persist_envelope_parse_failure(handle, failed_entry)` exists with the documented signature.
- [ ] OPEN command writeback creates position + thesis + bracket + entry order + protective leg orders; emits `order_submitted` + `thesis_created` + `capital_reserved`.
- [ ] CLOSE / ADJUST / CANCEL / ADD command writebacks emit the documented event chains.
- [ ] One `pm_decision` activity log entry per accepted envelope, detail carries envelope_id + modifications + command IDs.
- [ ] `command_abandoned` emission survives a per-command rollback (post-rollback transaction).
- [ ] `envelope_parse_failed` emission fires when `InvocationHandle` is present and Layer-1 parse fails; the in-memory `failed_submission_log` mutation also still happens (parallel surfaces).
- [ ] `submit_envelope_mcp.py` accepts an optional `InvocationHandle` and invokes the appropriate writeback per code path (acceptance vs Layer-1 failure).
- [ ] Existing tests at `tests/execution/oms/test_submit_envelope_mcp.py` still pass without modification (default-arg `None` preserves the in-memory-only path).
- [ ] The Alembic migration extending the `activity_log.event_type` CHECK constraint with `"envelope_parse_failed"` is idempotent.
- [ ] `tests/execution/state_persistence/test_phase2_write_path.py` covers each acceptance criterion above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/ tests/portfolio_state/ -n auto` passes (broader scope to verify the OMS-stub coordination did not break sibling tests).

## Verification

Run `uv run pytest tests/execution/ tests/portfolio_state/ -n auto`. Spot-check the OPEN-command test: assert that after writeback, `positions` has one row in PENDING, `theses` has one row in ACTIVE with components, `brackets` has one row in PENDING_ENTRY with all protective legs, `orders` has the entry + protective rows in PENDING, `cash_ledger.reserved_capital_usd` reflects the reservation, and `activity_log` contains the four documented entries plus the `pm_decision`. Spot-check the Layer-1 failure test: a malformed payload appends one entry to both `state.failed_submission_log` AND `activity_log` (event_type = `envelope_parse_failed`).