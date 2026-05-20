# 01 — Package skeleton + corporate-actions dispatch refactor

## Goal

Stand up `src/alphamind/execution/corporate_actions/` as a top-level peer package to `state_persistence/`, `broker_adapter/`, and `oms/`. Move the existing SPLIT-path code out of `src/alphamind/execution/state_persistence/write_paths/phase1.py` into the new package: `CorporateActionActivity` typed input, the `_integrate_one_ca_activity` orchestrator, `_apply_ca_to_quantity_and_basis` math, `_cancel_bracket_for_corporate_action` bracket lifecycle, and `_emit_corporate_action_applied` log emitter. Refactor the integration entry point into a per-action-type dispatch dict mapping every `CorporateActionType` member to a handler stub (every non-SPLIT entry raises `NotImplementedError` with the same message as today, preserving current behavior). Introduce an `AlpacaPositionLookup` Protocol-shaped callable for non-equity handlers to read post-adjustment Alpaca state, and a `CorporateActionsConfig` Pydantic model exposing the fetcher lookback window (default 7 days, consumed by story 02). Semantic behavior of the SPLIT path is unchanged after this story; all 03a-03d handler stories build directly on the dispatch table this story sets up.

## Reading

* `docs/design/05-execution-layer/corporate-actions.md` § Source of truth § Per-action-type matrix § Phase 1 integration sequence § Idempotency § Activity log catalog additions — the integration spec this package implements
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` lines 170-1612, with focus on the CA-integration cluster at lines 1480-1611 (`_integrate_one_ca_activity`, `_apply_ca_to_quantity_and_basis`, `_cancel_bracket_for_corporate_action`, `_emit_corporate_action_applied`, `CorporateActionActivity`) — code being relocated
* `src/alphamind/execution/state_persistence/write_paths/ca_integration_ledger.py` — the existing `mark_ca_activity_processed` helper that handlers still call
* `src/alphamind/execution/state_persistence/tables/corporate_action_integration_ledger.py` — dedup-anchor table
* `src/alphamind/portfolio_state/events/activity_log.py` — `CorporateActionType` enum members + `CorporateActionAppliedDetail`, `BracketCancelledCorporateActionDetail`, `EventSource.CORPORATE_ACTION_PROCESSOR`
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord` with `corporate_action_adjustment_needed`, `parent_position_id`, `origin`; spin-off invariant validator
* `tests/execution/state_persistence/test_phase1_write_path.py::test_corporate_action_split_emits_events_and_ledger_anchor` — the SPLIT-path coverage that must survive unchanged
* `ALP-365` (state-persistence story 07 / Phase 1 write path) — original SPLIT scaffold context
* `ALP-122` (Position & thesis model parent) — typed-records ownership

## Depends on

None — all sibling deps are already DONE ([ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119/state-persistence) State persistence, [ALP-122](https://linear.app/alphamind-jatassi/issue/ALP-122/position-and-thesis-model) Position & thesis model).

## Scope

In scope, all under `src/alphamind/execution/corporate_actions/`. Tests at `tests/execution/corporate_actions/`. The `state_persistence/write_paths/phase1.py` SPLIT-path entry point becomes a thin re-export shim.

### 1\. Package layout

Create the following files under `src/alphamind/execution/corporate_actions/`:

* `__init__.py` — re-export the public API (`integrate_ca_activity`, `CorporateActionActivity`, `AlpacaPositionLookup`, `CorporateActionsConfig`)
* `types.py` — moved `CorporateActionActivity` Pydantic model; new `AlpacaPositionLookup` Protocol with `get_position(symbol: str) -> PositionSnapshot | None` (from `broker_adapter.queries`)
* `config.py` — `CorporateActionsConfig(BaseModel, frozen=True)` with `fetcher_lookback_days: int = 7`
* `dispatch.py` — public `async def integrate_ca_activity(handle, activity, alpaca_position_lookup) -> None` + `_HANDLERS: dict[CorporateActionType, CAHandler]` dispatch dict with one entry per `CorporateActionType` enum member
* `handlers/__init__.py`
* `handlers/_shared.py` — moved `_cancel_bracket_for_corporate_action`, `_emit_corporate_action_applied`; new `_apply_signed_cash_movement(handle, signed_cash_impact_usd, *, reason) -> None` helper used by cash-impact handlers in 03b / 03c
* `handlers/splits.py` — moved equity SPLIT handler (the existing logic) + stub entries for `REVERSE_SPLIT`, `STOCK_DIVIDEND`, `SYMBOL_CHANGE`, `CASH_DIVIDEND_LONG`, `CASH_DIVIDEND_SHORT`, `CASH_MERGER`, `STOCK_MERGER`, `SPIN_OFF` (each raising `NotImplementedError("CA action_type=<name> not yet supported by Phase 1")`)

### 2\. Handler signature

`CAHandler = Callable[[InvocationHandle, CorporateActionActivity, AlpacaPositionLookup | None], Awaitable[None]]`. Equity-only handlers (today's SPLIT, future story 03b's cash dividends) ignore the `alpaca_position_lookup` argument and may type-annotate it as `_: AlpacaPositionLookup | None = None`. Options / strategy branches (03a-03d) call the lookup for non-equity positions.

### 3\. Phase 1 integration shim

`phase1.py::_integrate_one_ca_activity` becomes a thin shim that imports `integrate_ca_activity` from the new package and calls it with `alpaca_position_lookup=None` (story 04 wires the real lookup). Existing tests construct activities without options positions, so passing `None` keeps coverage green. `CorporateActionActivity` remains re-exportable from `phase1.py` for back-compat against any other importer in the codebase.

### 4\. Configuration plumbing

Add `corporate_actions: CorporateActionsConfig = CorporateActionsConfig()` to `StatePersistenceConfig` (or expose as a peer config that `state_persistence`'s loader composes — choose the path that keeps the loader cohesive). Story 02 consumes `fetcher_lookback_days`; no other story reads the config in this work tree.

### 5\. Test relocation

* Existing `tests/execution/state_persistence/test_phase1_write_path.py::test_corporate_action_split_emits_events_and_ledger_anchor` continues to pass via the shim — do not move it.
* Add `tests/execution/corporate_actions/__init__.py`, `_fk_substrate.py` (mirroring the state_persistence helper if useful), `test_dispatch.py`, `test_splits.py`.
* `test_dispatch.py` — for each `CorporateActionType` member except `SPLIT`, assert that `integrate_ca_activity(...)` raises `NotImplementedError` with the documented message.
* `test_splits.py` — duplicate the SPLIT happy path against the public `integrate_ca_activity` entry point with `alpaca_position_lookup=None` to assert the new entry point composes with the moved internals.

### Out of scope

Any non-SPLIT handler implementation (owned by 03a, 03b, 03c, 03d). The activity fetcher (story 02). Reworking Phase 1 chronological merge or adding reconciliation (story 04). Operator-facing verify script (story 05).

## Acceptance criteria

- [ ] `src/alphamind/execution/corporate_actions/` package exists with the file layout in Scope § 1.
- [ ] `CorporateActionActivity` is importable from `alphamind.execution.corporate_actions.types`; the back-compat re-export from `alphamind.execution.state_persistence.write_paths.phase1` continues to work.
- [ ] `integrate_ca_activity(handle, activity, alpaca_position_lookup)` is the public entry point on `corporate_actions.dispatch` and is re-exported from the package `__init__.py`.
- [ ] `AlpacaPositionLookup` Protocol is defined in `types.py` with one method `get_position(symbol: str) -> PositionSnapshot | None`.
- [ ] `CorporateActionsConfig.fetcher_lookback_days` defaults to `7` and is reachable from `StatePersistenceConfig` (or the peer loader).
- [ ] `_HANDLERS` dispatch dict has exactly one entry per `CorporateActionType` member (9 entries: SPLIT, REVERSE_SPLIT, STOCK_DIVIDEND, CASH_DIVIDEND_LONG, CASH_DIVIDEND_SHORT, CASH_MERGER, STOCK_MERGER, SPIN_OFF, SYMBOL_CHANGE).
- [ ] Each non-SPLIT dispatch entry raises `NotImplementedError("CA action_type=<name> not yet supported by Phase 1")` when invoked.
- [ ] `_apply_signed_cash_movement(handle, signed_cash_impact_usd, *, reason)` helper exists in `handlers/_shared.py` and updates `cash_ledger.current_cash_usd` plus emits one `CASH_CREDITED` / `CASH_DEBITED` activity-log entry (whichever the sign and reason imply).
- [ ] Existing `tests/execution/state_persistence/test_phase1_write_path.py::test_corporate_action_split_emits_events_and_ledger_anchor` passes unchanged.
- [ ] New `tests/execution/corporate_actions/test_dispatch.py` verifies every non-SPLIT `CorporateActionType` member dispatches to a stub that raises `NotImplementedError`.
- [ ] New `tests/execution/corporate_actions/test_splits.py` exercises `integrate_ca_activity` against a seeded equity position and asserts the same post-state as the original SPLIT test.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, and `uv run pytest -n auto` all pass.

## Verification

Run `uv run pytest -n auto tests/execution/corporate_actions/ tests/execution/state_persistence/test_phase1_write_path.py`. Inspect by opening `corporate_actions/dispatch.py` and confirming the `_HANDLERS` dict has 9 entries; 8 raise `NotImplementedError`, 1 (SPLIT) routes to the moved equity handler.