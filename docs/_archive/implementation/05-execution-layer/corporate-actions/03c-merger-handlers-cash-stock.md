# 03c — Merger handlers (cash + stock)

## Goal

Implement the `CASH_MERGER` and `STOCK_MERGER` handlers in `src/alphamind/execution/corporate_actions/handlers/mergers.py`. Cash mergers terminate the position: quantity zeroed, status → `CLOSED`, realized P/L accumulated from deal price vs. cost basis, deal proceeds credited via `CASH_CREDITED` with reason `CASH_MERGER_PROCEEDS`, position closed with `PositionExitMethod.CORPORATE_ACTION_CASH_MERGER`. Stock mergers swap the position to the acquirer: new ticker (`activity.new_ticker`), new quantity + cost basis from `AlpacaPositionLookup`, optional partial cash credit for cash-and-stock mixes. Both handlers cancel the bracket and write the dedup ledger row; cash mergers do NOT set `corporate_action_adjustment_needed` (position closed), stock mergers DO set it (strategist re-evaluates the new acquirer position per `strategist.md`'s default-close stance). Replaces the `NotImplementedError` stubs that story 01 wires into the dispatch dict for these two `CorporateActionType` members.

## Reading

* `docs/design/05-execution-layer/corporate-actions.md` § Per-action-type matrix — rows: *Cash merger / acquisition*, *Stock merger*
* `docs/design/04-decision-layer/strategist.md` § Corporate-action-pending positions — cash-merger default action = `close`; stock-merger default action = `close` unless fresh thesis on acquirer
* `docs/design/05-execution-layer/corporate-actions.md` § Options positions — mergers on optioned underlyings "messier"; Alpaca surfaces a sequence of activities; OMS applies post-adjustment state
* `src/alphamind/portfolio_state/events/activity_log.py` — `CashCreditReason.CASH_MERGER_PROCEEDS`, `PositionExitMethod.CORPORATE_ACTION_CASH_MERGER`, `PositionClosedDetail`
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` — existing `_dissolve_bracket` + `_emit` patterns for `POSITION_CLOSED` (the cash-merger handler mirrors)
* `src/alphamind/execution/corporate_actions/handlers/_shared.py` (after story 01) — `_apply_signed_cash_movement`, `_cancel_bracket_for_corporate_action`, `_emit_corporate_action_applied`

## Depends on

* `ALP-409` (story 01) — package layout, dispatch dict, shared helpers, `AlpacaPositionLookup` Protocol

## Scope

In scope: `src/alphamind/execution/corporate_actions/handlers/mergers.py`. Tests at `tests/execution/corporate_actions/test_mergers.py`. Story 01's dispatch dict gets two `NotImplementedError` stubs replaced — coordinated single-line edits to `dispatch.py`.

### 1\. Cash merger handler

`async def handle_cash_merger(handle, activity, lookup) -> None`:

* **Equity branch:** set `EquityPositionDetails.share_count = 0`; transition `status` to `CLOSED`; compute realized P/L from `(activity.signed_cash_impact_usd / pre_qty) - average_cost_basis_per_share` per share × pre_qty (or equivalent formulation that matches the existing `_apply_exit_fill` realized-P/L pattern), add to `realized_pnl_to_date_usd`.
* **Options branch:** close the option position; realized P/L is `activity.signed_cash_impact_usd - (contract_count × premium_paid_per_contract × contract_multiplier)`.
* **Strategy branch:** close all legs; realized P/L is the aggregate (option-equivalent formula per leg).
* Credit cash by `activity.signed_cash_impact_usd` via `_apply_signed_cash_movement(handle, ..., reason=CashCreditReason.CASH_MERGER_PROCEEDS)`.
* Emit `POSITION_CLOSED` with `exit_method=PositionExitMethod.CORPORATE_ACTION_CASH_MERGER`, `exit_price=activity.signed_cash_impact_usd / pre_qty` (deal price per share), `realized_pnl_usd=<computed>`.
* Emit `CORPORATE_ACTION_APPLIED` and `BRACKET_CANCELLED_CORPORATE_ACTION` per the shared mechanism.
* Do NOT set `corporate_action_adjustment_needed` — the position is closed and `_check_spinoff_invariant` plus other validators don't require it.
* Resolve linked thesis: if `position.thesis_id` is non-None, mark the thesis `RESOLVED` with `resolution_timestamp=activity.transaction_time` (mirrors `_maybe_resolve_thesis` in [phase1.py](<http://phase1.py>)).

### 2\. Stock merger handler

`async def handle_stock_merger(handle, activity, lookup) -> None`:

* **Equity branch:** call `lookup.get_position(activity.new_ticker)`; update `EquityPositionDetails.ticker` to `activity.new_ticker`, `share_count` to the looked-up `qty`, `average_cost_basis_per_share` to the looked-up `avg_entry_price` (Alpaca's allocation). If `activity.signed_cash_impact_usd > 0` (cash-and-stock mix), credit the partial cash via `_apply_signed_cash_movement` with `reason=CashCreditReason.CASH_MERGER_PROCEEDS` (the design table writes this row as a generic credit; the dedicated cash-and-stock-mix reason isn't named separately — reuse `CASH_MERGER_PROCEEDS`).
* **Options branch:** call `lookup.get_position(activity.new_ticker)` for the post-merger option contract; project the post-adjustment state onto `OptionsPositionDetails` (`strike_price`, `contract_count`, `contract_multiplier`, `underlying_ticker`, `premium_paid_per_contract` from `avg_entry_price`). Set greeks to `None`.
* **Strategy branch:** per-leg `lookup` projection.
* Set `corporate_action_adjustment_needed = True`.
* Emit `CORPORATE_ACTION_APPLIED` (carrying `new_ticker`), `BRACKET_CANCELLED_CORPORATE_ACTION`, optional `CASH_CREDITED` for the partial-cash leg.
* Call `mark_ca_activity_processed`.

### 3\. Shared mechanism

Both handlers:

1. Load the position; raise `ValueError` if missing.
2. Apply the per-action mutation.
3. Cash merger: do NOT set `corporate_action_adjustment_needed`. Stock merger: set it `True`.
4. Cancel the bracket via `_cancel_bracket_for_corporate_action`.
5. Emit `CORPORATE_ACTION_APPLIED`.
6. Call `mark_ca_activity_processed`.

### 4\. Dispatch wiring

Replace the two `NotImplementedError` stubs in `dispatch.py::_HANDLERS`:

```python
CorporateActionType.CASH_MERGER: handle_cash_merger,
CorporateActionType.STOCK_MERGER: handle_stock_merger,
```

### Out of scope

Reverse splits / stock dividends / ticker changes (03a). Cash dividends (03b). Spin-offs (03d). The fetcher (02). The chronological merge or reconciliation (04). Sequence ordering when Alpaca emits a stock merger as multiple paired `MA` + `NC` activities (surface to operator at implementation time if the handler design's single-activity assumption breaks).

## Acceptance criteria

- [ ] `handle_cash_merger(handle, activity, lookup)` sets `EquityPositionDetails.share_count = 0`, transitions `status` to `CLOSED`, and accumulates `realized_pnl_to_date_usd` from deal price vs. cost basis.
- [ ] Cash merger emits one `POSITION_CLOSED` entry with `exit_method=CORPORATE_ACTION_CASH_MERGER` and one `CASH_CREDITED` entry with `reason=CASH_MERGER_PROCEEDS`.
- [ ] Cash merger does NOT set `corporate_action_adjustment_needed` (position is closed).
- [ ] Cash merger resolves the linked thesis when `position.thesis_id` is non-None (status → `RESOLVED`, resolution timestamp set).
- [ ] `handle_stock_merger(handle, activity, lookup)` reads from `lookup.get_position(activity.new_ticker)` and updates `EquityPositionDetails.ticker` to `activity.new_ticker`, `share_count` to the looked-up qty, `average_cost_basis_per_share` to the looked-up avg entry price.
- [ ] Stock merger sets `corporate_action_adjustment_needed = True` and emits `CORPORATE_ACTION_APPLIED` carrying the new ticker.
- [ ] Stock merger credits partial cash via `CASH_CREDITED` when `activity.signed_cash_impact_usd > 0`.
- [ ] Both handlers cancel the bracket and write to the ledger.
- [ ] Options-branch handlers set greeks to `None`.
- [ ] `dispatch.py::_HANDLERS` routes `CASH_MERGER` and `STOCK_MERGER` to the two handlers (no more `NotImplementedError` for these).
- [ ] `tests/execution/corporate_actions/test_mergers.py` covers cash + stock variants for equity, options, and strategy positions.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run pytest -n auto tests/execution/corporate_actions/test_mergers.py`. Inspect by opening `handlers/mergers.py` and confirming the cash-vs-stock dispatch by `CorporateActionType` and the per-instrument branching.