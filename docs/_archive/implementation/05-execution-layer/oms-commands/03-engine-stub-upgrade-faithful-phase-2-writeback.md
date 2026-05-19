# 03 — Engine-stub upgrade: faithful Phase 2 writeback

## Goal

Refine the engine-stub `submit_envelope` MCP wrapper and the Phase 2 writeback machinery to consume canonical full OMS command models (story 01a) and persist real command fields to SQL — replacing the existing $1k/share token sizing with real values from `position_size`, `entry_order`, `target`, `invalidation_legs`, and `thesis`. After this story, an accepted PM envelope writes faithful state to `positions` / `theses` / `thesis_components` / `brackets` / `bracket_legs` / `orders` / `cash_ledger` / `activity_log` reflecting the actual PM intent. Broker routing remains deferred per parent decision (C); this story keeps the engine-stub stub-grade in that one dimension only.

## Reading

* `src/alphamind/execution/oms/submit_envelope_mcp.py` — the existing engine-stub wrapper; story 03 refines its imports and helpers in place
* `src/alphamind/execution/state_persistence/write_paths/phase2.py` — Phase 2 write path with `_writeback_open` / `_writeback_close` / `_writeback_adjust` / `_writeback_cancel` / `_writeback_add` and the four `_STUB_*` constants story 03 removes
* `src/alphamind/execution/oms/command_models.py` (story 01a) — canonical full OMS command models the writeback consumes
* `src/alphamind/execution/oms/command_ids.py` (story 01b) — `derive_pm_command_id` replaces the inline `_format_command_id` helper; `compute_attempt_seq` replaces the inline `_attempt_seq` helper
* `docs/design/05-execution-layer/state-persistence.md` § Phase 2 write path — per-command-type write sequences (OPEN / CLOSE / ADJUST / CANCEL / ADD); each numbered step maps to one or more activity-log emits
* `docs/design/05-execution-layer/oms-commands.md` § OPEN / CLOSE / ADJUST / CANCEL / ADD — design semantics for required parameters; the writeback maps these directly to persisted fields
* `src/alphamind/portfolio_state/records/positions.py` and `.theses` and `.orders` — persisted record shapes (`PositionRecord`, `ThesisRecord`, `OrderRecord`, `BracketRecord`, etc.) the writeback constructs from canonical commands
* `src/alphamind/portfolio_state/events/activity_log.py` — `ActivityLogEntry` and detail types (`OrderSubmittedDetail`, `ThesisCreatedDetail`, `CapitalReservedDetail`, `BracketModifiedDetail`, `PMDecisionDetail`, etc.) the writeback emits
* `tests/execution/oms/test_submit_envelope_mcp.py` (1373 lines) — the test surface that is updated alongside the code changes
* Parent issue <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> § Pre-resolved configuration decisions (C) and (G) — engine-stub scope and wire/persisted leg distinction

## Depends on

* <issue id="11c3d121-2d77-4218-9355-90a234d5ff7b">ALP-370</issue> (01a) — canonical full OMS command models
* <issue id="ad3764c3-3bd2-4cb2-a91b-b85f87fa86ba">ALP-371</issue> (01b) — command-ID derivation utility
* <issue id="bea56244-1d7f-4688-bf1d-e97c0f3468d6">ALP-373</issue> (02b) — PM imports canonical models; this story's engine-stub then uses canonical types end-to-end

## Scope

Two files dominate: `src/alphamind/execution/oms/submit_envelope_mcp.py` and `src/alphamind/execution/state_persistence/write_paths/phase2.py`. Tests at `tests/execution/oms/test_submit_envelope_mcp.py` and the corresponding test_phase2 surface.

### 1\. `submit_envelope_mcp.py` — consume canonical models

* Update imports: `from alphamind.execution.oms.command_models import OMSCommand, OpenCommand, CloseCommand, AdjustCommand, CancelCommand, AddCommand, ...` (or via the `alphamind.execution.oms` package's `__init__`). Drop transitional re-exports through `alphamind.decision.portfolio_manager`.
* Replace the inline `_format_command_id` helper with `from alphamind.execution.oms.command_ids import derive_pm_command_id`. Replace `_attempt_seq` with `compute_attempt_seq`.
* Update `_command_to_validation_request` and `_build_constructive_request`: read **real** `command.position_size.quantity` and `command.position_size.dollar_value` for `ValidationSize`, instead of the hardcoded `quantity=1, dollar_value=1_000.0`.
* Update `_build_acknowledgment` for OPEN/ADD: populate `validation_metadata.delta_adjusted_exposure` and `per_rule_headroom` from the real `result` (already does this; confirm no regressions).
* Update the docstring on `_handle_submit_envelope` to remove "engine-stub" caveats around stub sizing — keep the broker-routing-deferred caveat per parent decision (C).
* `submit-envelope-tool-schema.md` validation in tests: ensure response shape still matches the design schema.

### 2\. `state_persistence/write_paths/phase2.py` — faithful writeback

Remove the stub constants:

```python
_STUB_TOKEN_DOLLAR_VALUE = 1_000.0     # delete
_STUB_TOKEN_QUANTITY = 1.0              # delete
_STUB_FILL_PRICE = ...                   # delete
_STUB_STOP_PCT_BELOW_ENTRY = 0.05       # delete
```

Refresh each `_writeback_*` to consume real command fields:

* `_writeback_open(handle, command: OpenCommand, result)`:
  * Construct `PositionRecord(quantity=command.position_size.quantity, ...)` instead of `_STUB_TOKEN_QUANTITY`. Pull `dollar_value` for the position's notional.
  * Construct entry `OrderRecord(order_type=command.entry_order.type, limit_price=command.entry_order.limit_price, stop_price=command.entry_order.stop_price, quantity=command.position_size.quantity, ...)`.
  * Construct `BracketRecord` with one leg per `command.invalidation_legs[]` entry (translate wire `PriceLeg` / `TimeLeg` / `EventLeg` to persisted `BracketLeg` per parent decision (G)). Take-profit leg from `command.target`.
  * Construct `ThesisRecord(summary=command.thesis.summary, ...)` with `ThesisComponent` rows from `command.thesis.components[]`.
  * Reserve capital amount = `command.position_size.dollar_value`.
* `_writeback_close(handle, command: CloseCommand, result)`:
  * Read `command.quantity` (numeric or `"all"`) — handle the union: numeric → reduce position by quantity; `"all"` → mark for full close.
  * Read `command.order_type`, `command.limit_price`, `command.close_rationale_type`, `command.invalidation_reason`, `command.risk_management_subtype` — populate the close order, the activity-log `position_reduced` / `position_closed` detail, and the thesis resolution category.
* `_writeback_adjust(handle, command: AdjustCommand, result)`:
  * Read whichever of `new_stop_level` / `new_target_level` / `new_time_expiration` / `new_event_invalidation` / `thesis_component_updates` is set — translate each to the corresponding bracket-leg cancel + resubmit + bracket-modification activity-log entry. `command.adjustment_rationale` populates `BracketModifiedDetail.rationale`.
* `_writeback_cancel(handle, command: CancelCommand, result)`:
  * Read `command.order_id` and `command.cancel_reason`. Per design § CANCEL: if the cancelled order is an entry leg, cancel all bracket legs + resolve thesis as cancelled-never-entered + transition bracket to dissolved.
* `_writeback_add(handle, command: AddCommand, result)`:
  * Read `command.additional_quantity`, `command.additional_dollar_value`, `command.entry_order`, `command.thesis_addition_component`, optionally `command.bracket_adjustment`. Construct add-entry `OrderRecord`, append component to thesis, optionally cancel + resubmit modified bracket legs.
* The two existing entry points (`persist_envelope_outcome`, `persist_envelope_parse_failure`, `persist_envelope_rejection`) keep their current signatures — story 03 refines internals, not contracts.

### 3\. Tests

* `tests/execution/oms/test_submit_envelope_mcp.py`: every test that previously asserted stub values (`assert position.dollar_value == 1_000.0`) is updated to assert pass-through of real command values. Where tests construct envelopes, switch to canonical full OMS shapes.
* `tests/execution/state_persistence/write_paths/test_phase2.py` (or wherever the phase2 tests live): same — update every test that asserted `_STUB_*` values to assert real command pass-through.
* Run `uv run pytest tests/execution/ -n auto` — both engine-stub and phase2 surfaces.
* Run `uv run pytest tests/decision/portfolio_manager/ -n auto` — confirm no PM regression from sector-resolver change interaction.

### Out of scope

* Real broker routing — deferred to broker adapter work tree per parent decision (C). The engine-stub keeps producing synthetic order acknowledgments via the existing path.
* Engine envelope submission path (`submit_engine_envelope`) — story 04
* E2E verify script — story 05
* Any new public-API surface; this story is internal refinement only

## Acceptance criteria

- [ ] `src/alphamind/execution/oms/submit_envelope_mcp.py` imports OMS command types from `alphamind.execution.oms.command_models` (or via `alphamind.execution.oms`); does not import from `alphamind.decision.portfolio_manager`.
- [ ] `submit_envelope_mcp.py` uses `derive_pm_command_id` from `alphamind.execution.oms.command_ids`; the inline `_format_command_id` helper is removed.
- [ ] `submit_envelope_mcp.py` uses `compute_attempt_seq` from `alphamind.execution.oms.command_ids`; the inline `_attempt_seq` helper is removed.
- [ ] `_command_to_validation_request` / `_build_constructive_request` read `command.position_size.quantity` and `command.position_size.dollar_value` instead of literal `1` and `1_000.0`.
- [ ] `src/alphamind/execution/state_persistence/write_paths/phase2.py` does not define `_STUB_TOKEN_DOLLAR_VALUE`, `_STUB_TOKEN_QUANTITY`, `_STUB_FILL_PRICE`, or `_STUB_STOP_PCT_BELOW_ENTRY` constants.
- [ ] `_writeback_open` constructs `PositionRecord`, `OrderRecord`, `BracketRecord`, `ThesisRecord` from `command.position_size`, `command.entry_order`, `command.invalidation_legs`, `command.target`, `command.thesis` — every field traceable to a canonical command field.
- [ ] `_writeback_close` reads `command.quantity` (handles the `float | "all"` union), `command.order_type`, `command.limit_price`, `command.close_rationale_type`, `command.invalidation_reason`, `command.risk_management_subtype`.
- [ ] `_writeback_adjust` dispatches on whichever of the five optional change-fields is set on `AdjustCommand`; populates `BracketModifiedDetail.rationale` from `command.adjustment_rationale`.
- [ ] `_writeback_cancel` reads `command.order_id` and `command.cancel_reason`; if the cancelled order is an entry leg, the bracket transitions to dissolved and the thesis resolves as cancelled-never-entered.
- [ ] `_writeback_add` reads `command.additional_quantity`, `command.additional_dollar_value`, `command.entry_order`, `command.thesis_addition_component`, and optionally `command.bracket_adjustment`.
- [ ] Wire-format `InvalidationLeg` (story 01a) is translated to persisted `BracketLeg` (`alphamind.portfolio_state.records.orders`) inside the writeback per parent decision (G); the two types remain distinct.
- [ ] `uv run pytest tests/execution/ -n auto` passes.
- [ ] `uv run pytest tests/decision/portfolio_manager/ -n auto` passes (regression check).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/execution/ tests/decision/portfolio_manager/ -n auto` exits clean. `grep -r "_STUB_TOKEN_DOLLAR_VALUE\|_STUB_TOKEN_QUANTITY\|_STUB_FILL_PRICE\|_STUB_STOP_PCT_BELOW_ENTRY" src/alphamind/execution/state_persistence/` returns no matches. Sanity-check the engine-stub end-to-end via the existing test suite — the response shape should still match `submit-envelope-tool-schema.md`, but the persisted field values now reflect real PM intent rather than stub values.
