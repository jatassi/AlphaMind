# 03d — Spin-off handler (parent + child orphan)

## Goal

Implement the `SPIN_OFF` handler in `src/alphamind/execution/corporate_actions/handlers/spin_offs.py`. The parent equity position's cost basis is reduced by Alpaca's allocated portion (quantity unchanged); a new child `PositionRecord` is created representing the spun-off shares with `thesis_id=None`, `bracket_id=None`, `corporate_action_adjustment_needed=True`, `parent_position_id=<parent>`, and `origin=f"spin_off_from_{parent.position_id}"`. The spin-off invariant validator (`PositionRecord._check_spinoff_invariant`) enforces these together. Emit `POSITION_OPENED` for the child with `mechanism=SPIN_OFF_FROM_PARENT`, `CORPORATE_ACTION_APPLIED` for the parent, and `BRACKET_CANCELLED_CORPORATE_ACTION` for the parent's bracket. No cash impact on either. Replaces the `NotImplementedError` stub that story 01 wires into the dispatch dict for `CorporateActionType.SPIN_OFF`.

## Reading

* `docs/design/05-execution-layer/corporate-actions.md` § Per-action-type matrix — row: *Spin-off*
* `docs/design/05-execution-layer/corporate-actions.md` § Spin-off child positions — child invariants (`thesis_id`/`bracket_id` null, `origin` provenance string, `corporate_action_adjustment_needed=true`)
* `docs/design/05-execution-layer/position-model.md` § Corporate-action-pending positions — spin-off child as structural exception
* `docs/design/04-decision-layer/strategist.md` § Corporate-action-pending positions — spin-off default: parent `adjust-bracket`, child `close` unless fresh thesis
* `src/alphamind/portfolio_state/records/positions.py` — `_check_spinoff_invariant` validator that enforces the (`origin`, `parent_position_id`, `corporate_action_adjustment_needed`) triplet
* `src/alphamind/portfolio_state/events/activity_log.py` — `PositionOpenMechanism.SPIN_OFF_FROM_PARENT`, `PositionOpenedDetail.parent_position_id`
* `src/alphamind/execution/state_persistence/tables/positions.py` + `positions_codec.py` — `PositionRow` insertion path for the new child row

## Depends on

* `ALP-409` (story 01) — package layout, dispatch dict, shared helpers, `AlpacaPositionLookup` Protocol

## Scope

In scope: `src/alphamind/execution/corporate_actions/handlers/spin_offs.py`. Tests at `tests/execution/corporate_actions/test_spin_offs.py`. Story 01's dispatch dict gets one `NotImplementedError` stub replaced — a coordinated single-line edit to `dispatch.py`.

### 1\. Parent position update (equity)

* Load the parent position; assert it's `EquityPositionDetails` (raise `NotImplementedError` for non-equity parents — design doesn't pin down spin-off semantics on optioned underlyings).
* `share_count` unchanged.
* `average_cost_basis_per_share` reduced by Alpaca's allocated portion. The allocation source is `lookup.get_position(activity.ticker)` for the post-spin parent state (Alpaca's authoritative basis after the spin); the handler reads `avg_entry_price` from that snapshot and updates the local position.
* Set `corporate_action_adjustment_needed = True`.

### 2\. Child position creation

Construct a new `PositionRecord` representing the spun-off shares:

* `position_id`: generated UUID (uuid4 hex; the persistence layer's existing pattern).
* `thesis_id = None`.
* `bracket_id = None`.
* `status = PositionStatus.OPEN`.
* `direction = Direction.LONG` (spin-offs from long parents produce long children; non-LONG parent positions raise `NotImplementedError`).
* `entry_timestamp = activity.transaction_time`.
* `details`: a new `EquityPositionDetails` with `ticker = activity.new_ticker`, `share_count = <Alpaca's allocated child qty>`, `average_cost_basis_per_share = <Alpaca's allocated child basis>` — both sourced from `lookup.get_position(activity.new_ticker)`.
* `execution_history`: one synthetic `PositionFill` with `fill_timestamp=activity.transaction_time`, `fill_price=<allocated_basis>`, `fill_quantity=<allocated_qty>`, `slippage=0.0`, `fees=0.0`, `live_execution_estimate=None`.
* `realized_pnl_to_date_usd = None`.
* `corporate_action_adjustment_needed = True`.
* `parent_position_id = <parent.position_id>`.
* `origin = f"spin_off_from_{parent.position_id}"`.

The Pydantic `_check_spinoff_invariant` validator asserts the (`origin`, `parent_position_id`, `corporate_action_adjustment_needed`) triplet; constructing the record correctly is required for the validator to pass.

Persist via `handle.session.add(position_record_to_row(child))`.

### 3\. Activity log emission

* **Parent:**
  * `CORPORATE_ACTION_APPLIED` via `_emit_corporate_action_applied` with the parent's pre/post quantity (unchanged) and pre/post basis (reduced).
  * `BRACKET_CANCELLED_CORPORATE_ACTION` via `_cancel_bracket_for_corporate_action`.
* **Child:**
  * `POSITION_OPENED` with `mechanism=PositionOpenMechanism.SPIN_OFF_FROM_PARENT`, `parent_position_id=<parent>`, `thesis_id=None`, `bracket_id=None`, `ticker=activity.new_ticker`, `direction="LONG"`, `fill_price=<allocated_basis>`, `quantity=<allocated_qty>`. `source=EventSource.CORPORATE_ACTION_PROCESSOR`.

### 4\. Ledger

Single ledger row keyed on `activity.alpaca_activity_id` via `mark_ca_activity_processed`. The child's INSERT joins the same atomic transaction.

### 5\. Non-equity parent fallback

If `parent.details` is not `EquityPositionDetails`, raise `NotImplementedError("SPIN_OFF on non-equity parent position not supported")`. The design § Options positions describes optioned-underlying spin-offs as "messier" with no explicit spec; a follow-up captures the gap if encountered in practice.

### 6\. Dispatch wiring

Replace the `NotImplementedError` stub in `dispatch.py::_HANDLERS`:

```python
CorporateActionType.SPIN_OFF: handle_spin_off,
```

### Out of scope

Reverse splits / stock dividends / ticker changes (03a). Cash dividends (03b). Mergers (03c). The fetcher (02). The chronological merge or reconciliation (04). Optioned-underlying spin-offs (raise `NotImplementedError`).

## Acceptance criteria

- [ ] `handle_spin_off(handle, activity, lookup)` reduces parent `EquityPositionDetails.average_cost_basis_per_share` based on the Alpaca-allocated portion (read via `lookup`) and leaves parent `share_count` unchanged.
- [ ] Child `PositionRecord` is created with `thesis_id=None`, `bracket_id=None`, `corporate_action_adjustment_needed=True`, `origin="spin_off_from_<parent>"`, `parent_position_id=<parent>`.
- [ ] Child position satisfies `_check_spinoff_invariant` (validator does not raise).
- [ ] Child `details.ticker = activity.new_ticker` and `share_count` / `average_cost_basis_per_share` come from `lookup.get_position(activity.new_ticker)`.
- [ ] Child `execution_history` has one synthetic `PositionFill` reflecting the spin-off allocation.
- [ ] Parent emits one `CORPORATE_ACTION_APPLIED` entry and one `BRACKET_CANCELLED_CORPORATE_ACTION` entry.
- [ ] Child emits one `POSITION_OPENED` entry with `mechanism=SPIN_OFF_FROM_PARENT` and `parent_position_id=<parent>`.
- [ ] One ledger row written for `activity.alpaca_activity_id`.
- [ ] Non-equity parent positions raise `NotImplementedError("SPIN_OFF on non-equity parent position not supported")`.
- [ ] `dispatch.py::_HANDLERS` routes `SPIN_OFF` to `handle_spin_off` (no more `NotImplementedError` for this).
- [ ] `tests/execution/corporate_actions/test_spin_offs.py` covers the happy path (equity parent + equity child), the invariant assertion (child constructed correctly), and the non-equity-parent fallback.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run pytest -n auto tests/execution/corporate_actions/test_spin_offs.py`. Inspect the child position row in a seeded DB after running the handler — confirm `thesis_id IS NULL`, `bracket_id IS NULL`, `origin = "spin_off_from_<parent>"`, `parent_position_id = <parent>`.