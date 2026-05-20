# 04 — Chronological fill+CA merge + reconciliation alerts

## Goal

Rework `process_unprocessed_fills` to interleave fills + CA activities by timestamp (per `corporate-actions.md § Phase 1 integration sequence` step 2 — "Sort ascending. Ordering matters when a fill straddles an ex-date"). Add the reconciliation step that compares local state to caller-supplied `PositionSnapshot` and `TradeAccountSnapshot` after applying all events; emit a new `RECONCILIATION_ALERT` activity-log event for unexplained deltas. Auto-correction is deferred to [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) per parent issue Pre-resolved decision (A). The Phase 1 entry-point signature becomes `process_unprocessed_fills(handle, ca_activities, alpaca_positions, alpaca_account, *, config)`. After this story, the complete Phase 1 CA pipeline lands: fetcher (02) → merge → handlers (01 + 03a-d) → reconciliation.

## Reading

* `docs/design/05-execution-layer/corporate-actions.md` § Phase 1 integration sequence — chronological merge, reconciliation step, atomicity contract
* `docs/design/05-execution-layer/corporate-actions.md` § Idempotency — rollback semantics on per-CA failure
* `docs/design/mid-pipeline-failure-handling.md` — fail-closed atomic-transaction policy
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` — current `process_unprocessed_fills` entry point (lines 191-228) + `Phase1Summary.reconciliation_deltas` placeholder
* `src/alphamind/portfolio_state/events/activity_log.py` — `EventType` + `EventGroup` + `AnyDetailType` + `EVENT_TYPE_TO_DETAIL_CLASS` + `EVENT_TYPE_TO_GROUP` (new `RECONCILIATION_ALERT` event lands across all five)
* `src/alphamind/execution/broker_adapter/queries.py` — `PositionSnapshot` (fields: `symbol`, `qty`, `avg_entry_price`, `cost_basis`, `side`) and `TradeAccountSnapshot` (fields: `cash`, `equity`, `buying_power`, `regt_buying_power`, ...)
* `src/alphamind/execution/corporate_actions/fetcher.py` (story 02) — the activity tuple shape

## Depends on

* `ALP-409` (story 01) — package layout + public entry point
* `ALP-410` (story 02) — fetcher producing the CA activity tuple
* `ALP-411` (story 03a), `ALP-412` (story 03b), `ALP-413` (story 03c), `ALP-414` (story 03d) — every per-action handler must exist for the merge to dispatch correctly

## Scope

In scope: rework `src/alphamind/execution/state_persistence/write_paths/phase1.py` (signature change + chronological merge) + add `src/alphamind/execution/corporate_actions/reconciliation.py` + extend `src/alphamind/portfolio_state/events/activity_log.py` with `RECONCILIATION_ALERT` event type. Tests at `tests/execution/state_persistence/test_phase1_write_path.py` (extended) + `tests/execution/corporate_actions/test_reconciliation.py`.

### 1\. `RECONCILIATION_ALERT` event type

In `src/alphamind/portfolio_state/events/activity_log.py`:

* Add `EventType.RECONCILIATION_ALERT = "RECONCILIATION_ALERT"`.
* Add `EventGroup.RECONCILIATION = "RECONCILIATION"` (new group; reconciliation is its own concern, not a CA event).
* Add `ReconciliationAlertDetail(BaseModel)` with fields: `domain: Literal["position", "cash", "buying_power"]`, `field_name: str`, `local_value: float`, `alpaca_value: float`, `delta_description: str`.
* Add `ReconciliationAlertDetail` to `AnyDetailType`, `EVENT_TYPE_TO_DETAIL_CLASS`, and `EVENT_TYPE_TO_GROUP`.

### 2\. Chronological merge

In [phase1.py](<http://phase1.py>), replace the current "all fills, then all CAs" loop with `_iter_merged_events(fills, ca_activities)` yielding `(timestamp, event_or_activity)` tuples in ascending order. On exact timestamp tie: fills before CAs (CA activities are typically EOD-posted; fills are intra-day execution events; treating fills as earlier on ties matches market reality and yields deterministic ordering).

### 3\. New entry-point signature

```python
async def process_unprocessed_fills(
    handle: InvocationHandle,
    ca_activities: tuple[CorporateActionActivity, ...] = (),
    alpaca_positions: tuple[PositionSnapshot, ...] = (),
    alpaca_account: TradeAccountSnapshot | None = None,
    *,
    config: StatePersistenceConfig,
) -> Phase1Summary
```

Default values keep existing tests passing while still requiring the caller to opt-in to reconciliation by passing `alpaca_positions` + `alpaca_account`. Document in the docstring that production callers always pass all four; defaults exist only for unit tests pre-04.

### 4\. `alpaca_position_lookup` adapter

Build a `Callable[[str], PositionSnapshot | None]` from `alpaca_positions` (an in-memory symbol→snapshot dict). Pass into each CA handler via the existing `AlpacaPositionLookup` Protocol from story 01. Equity-only handlers ignore it; options/strategy branches in 03a/03c/03d call it.

### 5\. Per-event dispatch in the merged loop

* Fill events: existing `_integrate_one_fill(handle, fill)` path.
* CA activities: new `integrate_ca_activity(handle, activity, alpaca_position_lookup)` from story 01.
* Both share the surrounding `InvocationContext` transaction; an exception in either rolls back the entire batch.

### 6\. Reconciliation step

`async def reconcile(handle, alpaca_positions, alpaca_account) -> int` in `corporate_actions/reconciliation.py`:

* For each `PositionRow` in the DB with `status` in `(OPEN, PENDING)`: look up `alpaca_positions` by `symbol` (`EquityPositionDetails.ticker` or `OptionsPositionDetails.underlying_ticker`). Compare `share_count` / `contract_count` to `PositionSnapshot.qty`. Tolerance: `_QTY_EPSILON` (1e-9, mirroring [phase1.py](<http://phase1.py>)'s existing constant).
* Compare `cash_ledger.current_cash_usd` to `alpaca_account.cash`. Tolerance: `0.01` USD.
* Compare `cash_ledger.reserved_capital_usd` semantics to `alpaca_account.buying_power` only if a reasonable mapping exists; surface to operator at implementation time if the mapping is ambiguous.
* Emit one `RECONCILIATION_ALERT` activity-log entry per unexplained delta (within the documented tolerances). Auto-correction is NOT performed; local state remains as-is.
* Return the alert count.

### 7\. `Phase1Summary` update

Add `reconciliation_alerts: int` field; remove or repurpose `reconciliation_deltas: dict[str, float]` (currently a placeholder).

### Out of scope

Auto-correction of deltas (deferred to [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor) per parent Pre-resolved decision A). The fetcher itself (story 02 owns). New handlers (03a-03d). The E2E verify script (story 05).

## Acceptance criteria

- [ ] `RECONCILIATION_ALERT` event type is registered in `EventType`, with a new `EventGroup.RECONCILIATION` group.
- [ ] `ReconciliationAlertDetail` is added to `AnyDetailType`, `EVENT_TYPE_TO_DETAIL_CLASS`, and `EVENT_TYPE_TO_GROUP`.
- [ ] `process_unprocessed_fills(handle, ca_activities, alpaca_positions, alpaca_account, *, config)` is the new signature.
- [ ] Fills + CA activities interleave by `transaction_time` / `fill_timestamp` ascending; the existing SPLIT-only test (`test_corporate_action_split_emits_events_and_ledger_anchor`) passes unchanged after the signature update.
- [ ] A new test asserts that a fill timestamped *before* a CA's `transaction_time` integrates BEFORE the CA's mutation (post-state reflects pre-CA quantity at the fill, then post-CA quantity at the CA).
- [ ] A new test asserts that a fill timestamped *after* a CA integrates AFTER (post-action quantity / basis at the fill).
- [ ] Reconciliation emits one `RECONCILIATION_ALERT` per unexplained delta in position quantity, options contract count, or cash balance (within tolerances `_QTY_EPSILON` / `0.01` USD).
- [ ] `Phase1Summary.reconciliation_alerts` counts the emitted alerts.
- [ ] No auto-correction code path exists in this story (alerts are emitted but local state is not mutated to match Alpaca).
- [ ] `tests/execution/corporate_actions/test_reconciliation.py` exercises position-quantity, options-contract-count, and cash-balance deltas with both within-tolerance (no alert) and beyond-tolerance (one alert per delta) cases.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run pytest -n auto tests/execution/state_persistence/ tests/execution/corporate_actions/`. Inspect the merged-loop logic by reading the updated `process_unprocessed_fills`; confirm `_iter_merged_events` produces ascending-timestamp order with documented tie-break.