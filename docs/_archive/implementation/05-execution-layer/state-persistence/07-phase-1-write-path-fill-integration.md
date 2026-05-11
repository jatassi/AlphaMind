# 07 — Phase 1 write path: fill integration

## Goal

Ship `process_unprocessed_fills(handle)` — the atomic Phase 1 write path that drains every `processing_status = 'unprocessed'` fill record (and every unprocessed CA activity), integrates each into state (orders / positions / brackets / theses / cash_ledger / drawdown_state), emits the corresponding activity-log entries, and marks the fills `processed` — all in one transaction. After this story, the snapshot read in story 06 reflects all Phase 1 mutations and none of Phase 2.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path: fill integration — the nine-step integration sequence (validate → update order → update position → update bracket → update thesis → update cash → update derived aggregates → write activity log entries → mark fills processed)
* `docs/design/05-execution-layer/state-persistence.md` § Snapshot isolation — defines the Phase 1 commit / snapshot-read sequencing this story honors
* `docs/design/05-execution-layer/corporate-actions.md` — defines the per-action-type matrix the CA integration step follows
* `docs/design/05-execution-layer/regt-margin-attribution.md` — defines the Reg T attribution attached to each fill during Phase 1
* `src/alphamind/portfolio_state/events/activity_log.py` § `OrderFilledDetail`, `PositionOpenedDetail`, `PositionClosedDetail`, `PositionAddedDetail`, `PositionReducedDetail`, `BracketActivatedDetail`, `BracketCompletedDetail`, `BracketDissolvedDetail`, `BracketIncompleteWarningDetail`, `BracketCancelledCorporateActionDetail`, `ThesisResolvedDetail`, `CashDebitedDetail`, `CashCreditedDetail`, `CapitalReleasedDetail`, `MarginCallDetail`, `MarginLiquidationDetail`, `CorporateActionAppliedDetail` — the event detail shapes Phase 1 emits
* Story 03 (`append_activity_log_entry`) — the emission helper
* Story 02b (`InvocationContext`) — the transaction substrate
* Story 05 (`fill_records`, `corporate_action_integration_ledger`) — the input substrate

## Depends on

* <issue id="dde68c00-9c4c-44f4-a8ed-0b8f0e9a4ba9">ALP-364</issue> (this work tree, story 06) — the repository layer is needed for snapshot read after Phase 1 commits.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/write_paths/`. Tests at `tests/execution/state_persistence/`.

### 1\. Phase 1 entry point

In `src/alphamind/execution/state_persistence/write_paths/phase1.py`:

```python
async def process_unprocessed_fills(
    handle: InvocationHandle,
    *,
    config: StatePersistenceConfig,
) -> Phase1Summary:
    """Drain unprocessed fills + CA activities and integrate them atomically.

    Caller-supplied `handle` is the open InvocationContext transaction. All
    state mutations and activity-log emissions commit as part of that
    transaction; on exception, the surrounding context manager rolls back
    and fills remain unprocessed for the next invocation.
    """
```

Returns a `Phase1Summary(fills_processed, fills_quarantined, ca_activities_processed, reconciliation_deltas)`.

### 2\. Fill-integration sequence

For each unprocessed fill, in fill-timestamp order, execute the design doc's nine-step sequence:

1. **Validate** — order exists, fill quantity positive, price plausible. On failure, mark `processing_status = 'quarantined'`, log a warning, raise an alert; the fill is excluded from integration.
2. **Update order** — increment `filled_quantity`, recompute `average_fill_price`, update `remaining_quantity`, advance `status` (PENDING → PARTIALLY_FILLED → FILLED).
3. **Update position** — entry fills: PENDING → OPEN; ADD fills: increment quantity, recompute cost basis; exit fills: decrement quantity, compute realized P/L for the exited portion. Quantity zero → CLOSED.
4. **Update bracket** — equity brackets: record the per-leg transitions Alpaca emits; options brackets: record monitor-triggered close when applicable; full-close fills: cancel remaining bracket legs, mark bracket DISSOLVED.
5. **Update thesis** — on position closure, set thesis `resolution_timestamp` and `status = 'RESOLVED'`. Do not set `resolution_category` here (analysis pipeline owns that).
6. **Update cash ledger** — buy fills: debit cash, release reserved capital; sell fills: credit cash, record unsettled if applicable; margin updates per the new position state.
7. **Update derived aggregates** — recompute `drawdown_state` (current portfolio value, current drawdown, HWM if breached). `portfolio_pnl_inputs` is computed at read time per parent issue Pre-resolved decision (D), so no write here.
8. **Write activity log entries** — emit one entry per mutation; a single fill may generate `order_filled` + `position_opened` + `bracket_activated` + `cash_debited` + `capital_released` (an entry fill).
9. **Mark fill processed** — set `processing_status = 'processed'`, `processing_invocation_id`, `processing_timestamp`.

### 3\. CA integration sub-sequence

For each unprocessed Alpaca CA activity (drained from `GET /v2/account/activities`, dedupe-checked against `corporate_action_integration_ledger`), apply the per-action-type integration from `corporate-actions.md` (split / reverse-split / stock-dividend / cash-dividend-long / cash-dividend-short / cash-merger / stock-merger / spin-off / symbol-change). Emit the corresponding `corporate_action_applied` activity log entry plus standard lifecycle events (`position_closed`, `position_opened`, `cash_credited`, `cash_debited`, `bracket_cancelled_corporate_action` as applicable). Append to `corporate_action_integration_ledger`.

### 4\. Atomicity contract

The entire sequence runs inside `handle`'s open transaction. On exception:

* Transaction rolls back via `InvocationContext.__aexit__`.
* Fill records remain `unprocessed` (their PK rows are not modified — the row updates from steps 1–9 are inside the rollback).
* Activity log emissions inside the transaction are also rolled back.
* The `Phase1Summary` is not returned; the exception propagates.

### 5\. Tests

Tests at `tests/execution/state_persistence/test_phase1_write_path.py` cover:

* Happy-path entry fill: `unprocessed → processed`, position transitions PENDING → OPEN, cash debited, activity log carries `order_filled` + `position_opened` + `bracket_activated` + `capital_released` + `cash_debited` entries.
* Happy-path exit fill: position OPEN → CLOSED, realized P/L computed, bracket DISSOLVED, thesis RESOLVED.
* Multi-fill ordering: two unprocessed fills on the same order in fill-timestamp order produce the right cumulative state.
* CA integration happy path: a stock split's `corporate_action_applied` entry fires plus the lifecycle events.
* Atomicity: a synthetic exception mid-integration leaves all state untouched (fills still unprocessed, activity log empty for the rollback'd entries).
* Quarantine path: a fill with negative quantity is marked `quarantined` and excluded from integration without aborting the rest.

### Out of scope

* No PM command processing — story 08.
* No PortfolioStateSnapshot assembly — that's the assembler's job, called after this commits.
* No continuous-monitor integration — the monitor isn't built (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>); this story's tests construct unprocessed fills directly via `append_fill_record`.

## Acceptance criteria

- [ ] `process_unprocessed_fills(handle)` exists with the documented signature and returns a populated `Phase1Summary`.
- [ ] All nine integration steps fire in order for each fill, in fill-timestamp order.
- [ ] Activity log emissions for a single fill cover every state mutation that fill produced (entry fill: `order_filled` + `position_opened` + `bracket_activated` + `capital_released` + `cash_debited`).
- [ ] On clean exit, fill rows transition `unprocessed → processed` with non-null `processing_invocation_id` and `processing_timestamp`.
- [ ] On exception, the transaction rolls back; fills remain `unprocessed`; no activity log entries persist.
- [ ] CA integration emits `corporate_action_applied` plus standard lifecycle events; `corporate_action_integration_ledger` records the dedupe anchor.
- [ ] A quarantined fill (negative quantity, etc.) is excluded from integration without aborting the batch.
- [ ] `tests/execution/state_persistence/test_phase1_write_path.py` covers each acceptance criterion.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_phase1_write_path.py -n auto`. Spot-check the atomicity test: it must assert *both* (a) no fills transitioned to `processed` and (b) no activity log entries persist after the rollback.