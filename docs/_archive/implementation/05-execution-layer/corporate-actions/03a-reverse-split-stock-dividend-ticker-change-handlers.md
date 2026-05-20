# 03a — Reverse split + stock dividend + ticker change handlers

## Goal

Implement the per-action-type handlers for `REVERSE_SPLIT`, `STOCK_DIVIDEND`, and `SYMBOL_CHANGE` in three new files under `src/alphamind/execution/corporate_actions/handlers/`. Each handler covers both equity and options/strategy positions: equity branches compute the new state from `CorporateActionActivity.ratio_or_amount` and `activity.new_ticker`; options/strategy branches read post-adjustment state via the `AlpacaPositionLookup` helper (from story 01) and project the post-adjustment fields onto the local `OptionsPositionDetails` / per-leg `StrategyLeg.options`. All three set `corporate_action_adjustment_needed = True`, cancel the bracket via the shared `_cancel_bracket_for_corporate_action` helper, emit the `CORPORATE_ACTION_APPLIED` activity-log entry via `_emit_corporate_action_applied`, write the dedup ledger row, and (for reverse split with fractional cash-out) credit cash via `_apply_signed_cash_movement`. Replaces the `NotImplementedError` stubs that story 01 wires into the dispatch dict for these three `CorporateActionType` members.

## Reading

* `docs/design/05-execution-layer/corporate-actions.md` § Per-action-type matrix — rows: *Reverse stock split*, *Stock dividend*, *Symbol / name change*
* `docs/design/05-execution-layer/corporate-actions.md` § Options positions — Alpaca pre-adjusts strike / contract count / multiplier on splits and stock dividends; OMS reads post-adjustment from `GET /v2/positions`; greeks not migrated
* `src/alphamind/execution/corporate_actions/handlers/splits.py` (after story 01) — the existing forward-SPLIT handler is the pattern to mirror
* `src/alphamind/execution/corporate_actions/handlers/_shared.py` (after story 01) — `_cancel_bracket_for_corporate_action`, `_emit_corporate_action_applied`, `_apply_signed_cash_movement`
* `src/alphamind/portfolio_state/records/positions.py` — `EquityPositionDetails.ticker`, `OptionsPositionDetails.underlying_ticker` / `strike_price` / `contract_count` / `contract_multiplier`, `StrategyLeg.options`
* `src/alphamind/execution/broker_adapter/queries.py` — `PositionSnapshot` fields the `AlpacaPositionLookup.get_position(...)` returns
* `src/alphamind/portfolio_state/events/activity_log.py` — `CashCreditReason.FRACTIONAL_SHARE_CASH_OUT`, `CorporateActionType.REVERSE_SPLIT` / `STOCK_DIVIDEND` / `SYMBOL_CHANGE`
* `ALP-122` (Position & thesis model parent)

## Depends on

* `ALP-409` (story 01) — package layout, dispatch dict, shared helpers, `AlpacaPositionLookup` Protocol

## Scope

In scope: `src/alphamind/execution/corporate_actions/handlers/reverse_splits.py`, `handlers/stock_dividends.py`, `handlers/ticker_changes.py`. Tests at `tests/execution/corporate_actions/test_reverse_splits.py`, `test_stock_dividends.py`, `test_ticker_changes.py`. Story 01's dispatch dict gets three of its `NotImplementedError` stubs replaced with calls into these handlers — coordinated single-line edits to `dispatch.py`.

### 1\. Reverse split handler (`reverse_splits.py`)

* **Equity branch:** `share_count /= ratio_or_amount`; `average_cost_basis_per_share *= ratio_or_amount`. If `activity.signed_cash_impact_usd > 0` (Alpaca cashed out a fractional residual), call `_apply_signed_cash_movement(handle, activity.signed_cash_impact_usd, reason=CashCreditReason.FRACTIONAL_SHARE_CASH_OUT)`.
* **Options branch:** call `alpaca_position_lookup.get_position(activity.ticker)`; project `qty` → `OptionsPositionDetails.contract_count`, `avg_entry_price` → `premium_paid_per_contract` (Alpaca's avg cost basis is per-share-of-underlying; multiply by `contract_multiplier` for the per-contract premium). Strike + multiplier come from the Alpaca position snapshot (pre-adjusted by Alpaca). Set `delta`, `gamma`, `theta`, `vega` to `None` — stale per `architecture.md § 4d`.
* **Strategy branch:** for each leg, the same `alpaca_position_lookup` projection.
* Set `corporate_action_adjustment_needed = True` on the post-mutation position.

### 2\. Stock dividend handler (`stock_dividends.py`)

* **Equity branch:** `share_count *= (1 + ratio_or_amount)`; `average_cost_basis_per_share /= (1 + ratio_or_amount)`. No cash impact.
* **Options branch:** same `alpaca_position_lookup` flow as reverse split.
* **Strategy branch:** per-leg projection.
* Set `corporate_action_adjustment_needed = True`.

### 3\. Symbol / ticker change handler (`ticker_changes.py`)

* **Equity branch:** update `EquityPositionDetails.ticker` to `activity.new_ticker` (assert non-None).
* **Options branch:** update `OptionsPositionDetails.underlying_ticker` to `activity.new_ticker`; strike / contract count unchanged. (Greeks remain valid unless the underlying ticker change accompanies a structural change; assume not for `NC` / `SC`.)
* **Strategy branch:** update each leg's `underlying_ticker`.
* No quantity, basis, or cash impact.
* Set `corporate_action_adjustment_needed = True`.

### 4\. Shared mechanism for all three

Each handler:

1. Loads the position via `handle.session.get(PositionRow, activity.position_id)`; raises `ValueError` if missing.
2. Computes the post-mutation `PositionRecord` per the per-action branch above.
3. Sets `corporate_action_adjustment_needed = True`.
4. Calls `_persist_position_update(pos_row, updated)` (reusing the helper from `state_persistence.write_paths.phase1`; if not exported, expose it or replicate the projection logic).
5. Calls `_emit_corporate_action_applied(handle, activity=activity, position=updated, pre_qty=..., post_qty=..., pre_basis=..., post_basis=...)`.
6. Calls `_cancel_bracket_for_corporate_action(handle, position.bracket_id, activity)`.
7. Calls `mark_ca_activity_processed(handle, activity.alpaca_activity_id, processing_timestamp=activity.transaction_time)`.

### 5\. Dispatch wiring

Replace the three `NotImplementedError` stubs in `dispatch.py::_HANDLERS` with:

```python
CorporateActionType.REVERSE_SPLIT: handle_reverse_split,
CorporateActionType.STOCK_DIVIDEND: handle_stock_dividend,
CorporateActionType.SYMBOL_CHANGE: handle_symbol_change,
```

### Out of scope

Cash dividend handlers (03b). Merger handlers (03c). Spin-off handler (03d). The fetcher (02). The chronological merge or reconciliation (04). The e2e verify script (05).

## Acceptance criteria

- [ ] `handle_reverse_split(handle, activity, lookup)` applies `share_count /= ratio_or_amount` and `cost_basis *= ratio_or_amount` for equity positions.
- [ ] `handle_reverse_split` credits cash via `CASH_CREDITED` with reason `FRACTIONAL_SHARE_CASH_OUT` when `activity.signed_cash_impact_usd > 0`.
- [ ] `handle_reverse_split` on options positions reads post-adjustment state from `lookup.get_position(activity.ticker)` and projects to `OptionsPositionDetails`; greeks fields are `None` post-call.
- [ ] `handle_stock_dividend(handle, activity, lookup)` applies `share_count *= (1 + ratio_or_amount)` and `cost_basis /= (1 + ratio_or_amount)` for equity positions.
- [ ] `handle_stock_dividend` on options positions follows the same `lookup` projection as reverse split.
- [ ] `handle_symbol_change(handle, activity, lookup)` updates `EquityPositionDetails.ticker` to `activity.new_ticker` (equity) or `OptionsPositionDetails.underlying_ticker` (options) without changing quantity or basis.
- [ ] All three handlers set `corporate_action_adjustment_needed = True` on the post-mutation position.
- [ ] All three handlers emit exactly one `CORPORATE_ACTION_APPLIED` activity-log entry with the post-mutation payload.
- [ ] All three handlers emit exactly one `BRACKET_CANCELLED_CORPORATE_ACTION` activity-log entry and dissolve the position's bracket.
- [ ] All three handlers write exactly one `corporate_action_integration_ledger` row via `mark_ca_activity_processed`.
- [ ] Options branches set greeks (`delta`, `gamma`, `theta`, `vega`) to `None` post-application.
- [ ] Strategy branches apply per-leg projections, with one log entry per CA (not per leg).
- [ ] `dispatch.py::_HANDLERS` routes `REVERSE_SPLIT`, `STOCK_DIVIDEND`, `SYMBOL_CHANGE` to the three handlers (no more `NotImplementedError` for these).
- [ ] Per-handler test in `tests/execution/corporate_actions/` covers equity + options + strategy paths using a mock `AlpacaPositionLookup`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run pytest -n auto tests/execution/corporate_actions/test_reverse_splits.py tests/execution/corporate_actions/test_stock_dividends.py tests/execution/corporate_actions/test_ticker_changes.py`. Inspect by opening each handler file and confirming the per-branch dispatch on `isinstance(details, EquityPositionDetails / OptionsPositionDetails / StrategyPositionDetails)`.