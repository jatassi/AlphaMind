# 02 — Activity fetcher + Alpaca-code dispatcher

## Goal

Ship `src/alphamind/execution/corporate_actions/fetcher.py::fetch_unprocessed_ca_activities(handle, queries, *, config, position_id_for_symbol) -> tuple[CorporateActionActivity, ...]` — the function the Phase 1 caller invokes to pull new Alpaca CA activities, transform each into a `CorporateActionActivity`, and skip any whose `alpaca_activity_id` is already in `corporate_action_integration_ledger`. Implements the activity-type allowlist (`SPLIT`, `DIV`, `PTC`, `MA`, `SPIN`, `NC`, `SC`) and the Alpaca-code → `CorporateActionType` discriminator. Cursor derives from `max(processing_timestamp)` in the ledger minus `config.fetcher_lookback_days` (default 7); on a cold start the cursor is the lookback window backward from now. After this story, the fetcher is independently unit-testable; story 04 wires its output into the Phase 1 entry point.

## Reading

* `docs/design/05-execution-layer/corporate-actions.md` § Source of truth — Alpaca code mapping table; activity-type column of the per-action-type matrix
* `docs/design/05-execution-layer/corporate-actions.md` § Phase 1 integration sequence — step 1 (Query Alpaca for unprocessed activities)
* `src/alphamind/execution/broker_adapter/queries.py` — `AccountStateQueries.get_account_activities(...)` at line 443 (async generator over `ActivitySnapshot`); the `ActivitySnapshot` Pydantic record at lines 133-148 (fields: `id`, `activity_type`, `transaction_time`, `symbol`, `qty`, `price`, `net_amount`, `side`, `description`, `raw: dict[str, Any]`)
* `src/alphamind/execution/state_persistence/tables/corporate_action_integration_ledger.py` — dedup-key + `processing_timestamp` columns
* `src/alphamind/execution/corporate_actions/types.py` (story 01) — `CorporateActionActivity` typed input + `CorporateActionsConfig`
* `ALP-379` (Broker adapter story 02a / Account state GET wrappers) — Alpaca-side shape; `ActivityType` enum values from alpaca-py

## Depends on

* `ALP-409` (story 01) — `CorporateActionActivity`, `CorporateActionsConfig.fetcher_lookback_days`, package layout
* `ALP-379` (Broker adapter / Account state GET wrappers, *done*) — `AccountStateQueries.get_account_activities`, `ActivitySnapshot`

## Scope

In scope: `src/alphamind/execution/corporate_actions/fetcher.py`. Tests at `tests/execution/corporate_actions/test_fetcher.py`.

### 1\. `fetch_unprocessed_ca_activities` signature

```python
async def fetch_unprocessed_ca_activities(
    handle: InvocationHandle,
    queries: AccountStateQueries,
    *,
    config: CorporateActionsConfig,
    position_id_for_symbol: Callable[[str], str | None],
) -> tuple[CorporateActionActivity, ...]
```

Returns CA activities sorted ascending by `transaction_time`. Filters out activities for symbols with no matching local position via `position_id_for_symbol` (story 04 supplies the lookup from the snapshot assembler; tests inject a dict-backed callable).

### 2\. Activity-type allowlist

Module-level constant `_CA_ACTIVITY_TYPES: frozenset[str] = frozenset({"SPLIT", "DIV", "PTC", "MA", "SPIN", "NC", "SC"})`. Fetcher passes `tuple(_CA_ACTIVITY_TYPES)` as the `activity_types` kwarg to `queries.get_account_activities(...)`.

### 3\. Cursor derivation

`async def _derive_cursor(handle: InvocationHandle, *, lookback_days: int) -> datetime`:

* Query `select(func.max(CorporateActionIntegrationLedgerRow.processing_timestamp))` from the ledger.
* If the result is `None` (cold start): return `datetime.now(UTC) - timedelta(days=lookback_days)`.
* If a value exists: parse the ISO string, return that timestamp minus `timedelta(days=lookback_days)`. The lookback absorbs Alpaca's late posts; `alpaca_activity_id` dedup handles the resulting overlap.

The fetcher passes `after=<cursor>.isoformat()` (or whatever the `get_account_activities` `after` kwarg expects — verify against the alpaca-py contract at implementation time).

### 4\. Alpaca code → `CorporateActionType` discriminator

`def _dispatch_activity_type(snapshot: ActivitySnapshot) -> CorporateActionType`:

* `SPLIT`: read the ratio from `snapshot.raw["ratio_numerator"] / snapshot.raw["ratio_denominator"]` (or `snapshot.qty / pre_split_qty` per alpaca-py's surfacing). If `ratio > 1`: `SPLIT` (forward). If `ratio < 1`: `REVERSE_SPLIT`. Surface to operator at implementation time if alpaca-py does not surface a ratio numerator / denominator on `NonTradeActivity.raw`.
* `DIV` with a stock-distribution flag (alpaca-py's `NonTradeActivity` has a sub-type discriminator; verify the field name at impl time): `STOCK_DIVIDEND`. With a cash flag: `CASH_DIVIDEND_LONG`.
* `PTC`: `CASH_DIVIDEND_SHORT` (short-dividend pass-through).
* `MA` with stock consideration (sub-code or `raw` field): `STOCK_MERGER`. With cash consideration: `CASH_MERGER`.
* `SPIN`: `SPIN_OFF`.
* `NC`, `SC`: `SYMBOL_CHANGE`.

Where alpaca-py's surface for a sub-discrimination is unclear, the implementation must surface to operator rather than guess.

### 5\. `CorporateActionActivity` construction

`_build_activity(snapshot, ca_type, position_id_for_symbol) -> CorporateActionActivity | None`:

* Resolve local `position_id` via `position_id_for_symbol(snapshot.symbol)`. If `None`, return `None` (activity refers to a symbol AlphaMind has no exposure to).
* Populate `alpaca_activity_id = snapshot.id`, `action_type = ca_type`, `ticker = snapshot.symbol`, `transaction_time = snapshot.transaction_time`.
* `new_ticker`: for `NC` / `SC` and stock-merger `MA`, read from `snapshot.raw["new_symbol"]` (verify name at impl time). For other types: `None`.
* `ratio_or_amount`: for `SPLIT` / `REVERSE_SPLIT` / `STOCK_DIVIDEND`, the ratio (forward) or `(1 + rate)` (stock dividend). For `SPIN_OFF`, the per-share allocation. For other types: `0.0` or the activity-appropriate quantity field.
* `signed_cash_impact_usd`: for `DIV` (long): `snapshot.net_amount` (positive). For `PTC`: `snapshot.net_amount` (negative). For `MA` cash consideration: `snapshot.net_amount` (positive). For reverse-split fractional cash-out (signaled on the `SPLIT` activity's `raw`): the cash residual. For other types: `0.0`.

### 6\. Dedup against ledger

`async def _already_processed(handle: InvocationHandle, alpaca_activity_id: str) -> bool` — uses `handle.session.get(CorporateActionIntegrationLedgerRow, alpaca_activity_id)`. Activities already in the ledger are skipped.

### 7\. Sorting

Final output is sorted ascending by `transaction_time` so the Phase 1 chronological merge (story 04) can interleave with fills correctly.

### Out of scope

The position-lookup callable (story 04 wires it from the snapshot assembler). Reconciliation (story 04). Handler logic for the action types this fetcher produces (stories 03a-03d, plus 01's SPLIT path).

## Acceptance criteria

- [ ] `fetch_unprocessed_ca_activities` is importable from `alphamind.execution.corporate_actions.fetcher` and returns `tuple[CorporateActionActivity, ...]` sorted by `transaction_time` ascending.
- [ ] The fetcher passes the allowlist `("SPLIT", "DIV", "PTC", "MA", "SPIN", "NC", "SC")` to `AccountStateQueries.get_account_activities`.
- [ ] Cold-start cursor returns `now() - timedelta(days=config.fetcher_lookback_days)`.
- [ ] Warm-start cursor returns `max(processing_timestamp) - timedelta(days=config.fetcher_lookback_days)`.
- [ ] Activities already in `corporate_action_integration_ledger` (by `alpaca_activity_id`) are filtered out.
- [ ] `_dispatch_activity_type` correctly routes each Alpaca code to the expected `CorporateActionType` member; one parametric test per code (SPLIT forward, SPLIT reverse, DIV stock, DIV cash, PTC, MA cash, MA stock, SPIN, NC, SC).
- [ ] Activities for symbols with no matching local position return `None` from `_build_activity` and are filtered out of the final tuple.
- [ ] `tests/execution/corporate_actions/test_fetcher.py` exercises the full fetch path against a mock `AccountStateQueries` yielding seeded `ActivitySnapshot` records, plus a seeded ledger row to assert dedup.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run pytest -n auto tests/execution/corporate_actions/test_fetcher.py`. Inspect the allowlist constant and the `_dispatch_activity_type` discriminator by reading `fetcher.py`.