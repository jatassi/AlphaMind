# 03b — Cash dividend handlers (long + short obligation)

## Goal

Implement the per-action-type handlers for `CASH_DIVIDEND_LONG` and `CASH_DIVIDEND_SHORT` (short-dividend obligation pass-through) in `src/alphamind/execution/corporate_actions/handlers/cash_dividends.py`. Both handlers apply `activity.signed_cash_impact_usd` to the cash ledger via the shared `_apply_signed_cash_movement` helper (from story 01), emit a `CASH_CREDITED` (long) or `CASH_DEBITED` (short obligation) activity-log entry, leave quantity and cost basis unchanged, set `corporate_action_adjustment_needed = True` so the strategist re-evaluates per `strategist.md`'s cash-dividend default action (`adjust-bracket`), and cancel the bracket per the cancel-and-review policy. Replaces the `NotImplementedError` stubs that story 01 wires into the dispatch dict for these two `CorporateActionType` members.

## Reading

* `docs/design/05-execution-layer/corporate-actions.md` § Per-action-type matrix — rows: *Cash dividend (long)*, *Cash dividend (short obligation)*
* `docs/design/04-decision-layer/strategist.md` § Corporate-action-pending positions — cash-dividend default action; rationale for `adjust-bracket`
* `src/alphamind/portfolio_state/events/activity_log.py` — `CashCreditReason.CASH_DIVIDEND_LONG`, `CashDebitReason.CASH_DIVIDEND_SHORT_OBLIGATION`, `CashCreditedDetail`, `CashDebitedDetail`
* `src/alphamind/execution/corporate_actions/handlers/_shared.py` (after story 01) — `_apply_signed_cash_movement`, `_cancel_bracket_for_corporate_action`, `_emit_corporate_action_applied`

## Depends on

* `ALP-409` (story 01) — package layout, dispatch dict, shared helpers (including `_apply_signed_cash_movement`)

## Scope

In scope: `src/alphamind/execution/corporate_actions/handlers/cash_dividends.py`. Tests at `tests/execution/corporate_actions/test_cash_dividends.py`. Story 01's dispatch dict gets two `NotImplementedError` stubs replaced — coordinated single-line edits to `dispatch.py`.

### 1\. Cash dividend (long) handler

`async def handle_cash_dividend_long(handle, activity, lookup) -> None`:

* **Equity (long position):** credit cash by `activity.signed_cash_impact_usd` (positive) via `_apply_signed_cash_movement(handle, activity.signed_cash_impact_usd, reason=CashCreditReason.CASH_DIVIDEND_LONG)`. The shared helper emits one `CASH_CREDITED` activity-log entry with `reason=CASH_DIVIDEND_LONG`.
* **Options branch:** same — options positions on dividend-paying underlyings can receive a long dividend equivalent (rare; Alpaca passes through). Cash credit applies the same way; no `lookup` call needed for the cash-only mutation.
* **Strategy branch:** same; the cash credit is on the strategy as a whole.
* No change to `share_count` / `contract_count` / per-leg counts or cost basis.
* Set `corporate_action_adjustment_needed = True` on the position.

### 2\. Cash dividend (short obligation) handler

`async def handle_cash_dividend_short(handle, activity, lookup) -> None`:

* **Equity (short position):** debit cash by `abs(activity.signed_cash_impact_usd)` (input is negative) via `_apply_signed_cash_movement(handle, activity.signed_cash_impact_usd, reason=CashDebitReason.CASH_DIVIDEND_SHORT_OBLIGATION)`. The shared helper detects the negative sign and emits one `CASH_DEBITED` activity-log entry with `reason=CASH_DIVIDEND_SHORT_OBLIGATION`.
* Short positions are equity-only in AlphaMind today (per `position-model.md`); the handler can assert `position.direction == Direction.SHORT` for clarity, but the cash movement logic is identical.
* No change to `share_count` or cost basis.
* Set `corporate_action_adjustment_needed = True`.

### 3\. Shared mechanism (both handlers)

Each handler:

1. Loads the position; raises `ValueError` if missing.
2. Applies the cash movement via `_apply_signed_cash_movement`.
3. Sets `corporate_action_adjustment_needed = True`.
4. Calls `_emit_corporate_action_applied` with the unchanged pre/post quantity and basis values.
5. Calls `_cancel_bracket_for_corporate_action`.
6. Calls `mark_ca_activity_processed`.

### 4\. Dispatch wiring

Replace the two `NotImplementedError` stubs in `dispatch.py::_HANDLERS`:

```python
CorporateActionType.CASH_DIVIDEND_LONG: handle_cash_dividend_long,
CorporateActionType.CASH_DIVIDEND_SHORT: handle_cash_dividend_short,
```

### Out of scope

Reverse splits / stock dividends / ticker changes (03a). Mergers (03c). Spin-offs (03d). The fetcher (02). The chronological merge or reconciliation (04).

## Acceptance criteria

- [ ] `handle_cash_dividend_long(handle, activity, lookup)` credits cash by `activity.signed_cash_impact_usd` and emits one `CASH_CREDITED` activity-log entry with `reason=CASH_DIVIDEND_LONG`.
- [ ] `handle_cash_dividend_short(handle, activity, lookup)` debits cash by `abs(activity.signed_cash_impact_usd)` and emits one `CASH_DEBITED` activity-log entry with `reason=CASH_DIVIDEND_SHORT_OBLIGATION`.
- [ ] Both handlers leave `EquityPositionDetails.share_count` and `average_cost_basis_per_share` unchanged.
- [ ] Both handlers set `corporate_action_adjustment_needed = True` on the position.
- [ ] Both handlers emit exactly one `CORPORATE_ACTION_APPLIED` activity-log entry.
- [ ] Both handlers emit exactly one `BRACKET_CANCELLED_CORPORATE_ACTION` activity-log entry and dissolve the bracket.
- [ ] Both handlers write exactly one `corporate_action_integration_ledger` row via `mark_ca_activity_processed`.
- [ ] `dispatch.py::_HANDLERS` routes `CASH_DIVIDEND_LONG` and `CASH_DIVIDEND_SHORT` to the two handlers (no more `NotImplementedError` for these).
- [ ] `tests/execution/corporate_actions/test_cash_dividends.py` covers the long path (positive `signed_cash_impact_usd`) and the short path (negative `signed_cash_impact_usd`), each verifying cash-ledger delta, activity-log entries, and ledger row.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run pytest -n auto tests/execution/corporate_actions/test_cash_dividends.py`. Inspect by opening `handlers/cash_dividends.py` and confirming the two handlers share the `_apply_signed_cash_movement` call with different reasons.