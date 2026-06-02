# 06a — Phase 1 wiring (populate `FillRecord.regt_attribution`)

## Goal

Wedge per-fill attribution into `process_unprocessed_fills` so each integrated fill carries a populated `regt_attribution` value in `fill_records.regt_attribution_json`. Add a required `market_inputs: MarketInputs` parameter to the function; capture pre-fill positions, integrate the fill via the existing `_integrate_one_fill`, capture post-fill positions, call `compute_attribution(...)`, and persist via the existing codec round-trip. Migrate every existing Phase 1 / fill-record test fixture to thread a minimal `MarketInputs`. Quarantined fills retain `regt_attribution=None` per parent decision (H).

## Reading

* `src/alphamind/execution/state_persistence/write_paths/phase1.py` — `process_unprocessed_fills`, `_integrate_one_fill`, `_quarantine_invalid`, `_mark_processed`; identify the per-fill loop body that needs wedging.
* `src/alphamind/execution/state_persistence/tables/fill_records_codec.py` — `record_to_row` already encodes `regt_attribution.model_dump_json()` to `regt_attribution_json`; reuse without changes.
* `src/alphamind/execution/state_persistence/write_paths/records.py:52` — `RegTMarginAttribution` and `FillRecord.regt_attribution` shape.
* `src/alphamind/execution/regt_margin_attribution/orchestrator.py` (from story 05) — `compute_attribution` is the wedge point.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` § `MarketInputs` — typed input plumbed through.
* `tests/execution/state_persistence/test_phase1_write_path.py`, `test_phase1_options.py`, `test_phase1_strategy.py`, `test_fill_records_table.py`, `test_six_step_contract.py`, `test_sql_repository.py`, `test_phase2_write_path.py`, `src/alphamind/scripts/verify_state_persistence.py` — every existing call site that constructs a `FillRecord` or invokes `process_unprocessed_fills`; these need test-fixture migration.
* `ALP-126` parent issue § Pre-resolved decisions (F), (H) — required `market_inputs` parameter; null attribution on quarantined fills.

## Depends on

* `ALP-427` (05 — Per-fill attribution orchestrator) — provides `compute_attribution`.

## Scope

In scope: `src/alphamind/execution/state_persistence/write_paths/phase1.py` (signature + wedge); test-fixture migration across the listed test files; `src/alphamind/scripts/verify_state_persistence.py` updates. New tests for the wedge live in `tests/execution/state_persistence/test_phase1_regt_attribution.py`.

### 1\. Add `market_inputs` parameter

Add `market_inputs: MarketInputs` as a required keyword-only parameter on `process_unprocessed_fills`. Position it after `alpaca_account` and before `config` to keep `config` last by convention.

### 2\. Wedge attribution into the fill-integration loop

In the merged-events loop in `process_unprocessed_fills`, around each `_integrate_one_fill` call (only for `FillRecord` branches, not CA activities):

1. Before `_integrate_one_fill`: load pre-fill positions via a new private helper `_read_all_positions(handle) -> tuple[PositionRecord, ...]`.
2. Call `_integrate_one_fill(handle, event)`.
3. Load post-fill positions via the same helper.
4. Call `compute_attribution(pre_fill_positions=..., post_fill_positions=..., market_inputs=market_inputs, config=...)`.
5. Encode `attribution.model_dump_json()` into the fill record's `regt_attribution_json` column via direct SQL update on the `FillRecordRow` already loaded (the row instance is in `rows_by_fill_id`).
6. Then `_mark_processed(rows_by_fill_id[event.fill_id], handle.invocation_id)` as today.

Quarantined fills (rejected by `_quarantine_invalid`) skip attribution entirely — they are already excluded from the merged events loop.

Pass `RegTMarginAttributionConfig` through the function signature alongside `StatePersistenceConfig`, OR load it once at the top of the function via `load_regt_margin_attribution_config()`. Choose the path that requires fewer test surface changes; document the choice in the function docstring.

### 3\. `_read_all_positions` helper

A small private helper in `phase1.py` that queries `PositionRow` and rehydrates each via `position_row_to_record`. Returns all positions regardless of status (the attribution math filters internally). Called twice per fill — cheap; no caching.

### 4\. Test-fixture migration

Walk each existing test file listed in Reading. For each `process_unprocessed_fills(...)` call site, add `market_inputs=MarketInputs(underlying_prices={...}, risk_free_rate=0.0425, iv_provider=FixtureIvProvider(...), as_of=...)`. For tests that produce no options class groups, `underlying_prices` can include only the symbols of equity positions; `iv_provider` can be a `FixtureIvProvider` with empty seed (it will never be called).

For each existing `FillRecord(regt_attribution=None, ...)` test fixture, leave `regt_attribution=None` — those tests document the codec round-trip with null attribution, which remains a valid shape for quarantined fills (and tests that never run Phase 1).

For `verify_state_persistence.py`, construct a representative `MarketInputs` from a fixture set at script start; document this in the script's leading comment block.

### 5\. New tests (`test_phase1_regt_attribution.py`)

* `test_processed_fill_carries_populated_attribution` — seed an unprocessed fill, run Phase 1 with a non-empty `market_inputs`; assert the row's `regt_attribution_json` is non-null and round-trips to a `RegTMarginAttribution` with all 8 fields finite.
* `test_quarantined_fill_retains_null_attribution` — seed an invalid fill (e.g., negative quantity); assert quarantine and `regt_attribution_json IS NULL`.
* `test_batched_fills_have_threaded_pre_state` — seed two unprocessed fills on the same underlying; assert fill 2's `regt_margin_before` equals fill 1's `regt_margin_after` (the batched-fills determinism the design specifies).
* `test_phase1_summary_unchanged` — assert the `Phase1Summary` shape (the four count fields) is unchanged by the wedge.
* `test_missing_market_inputs_for_underlying_propagates_key_error` — fill whose symbol is missing from `market_inputs.underlying_prices` propagates `KeyError` (caller is responsible for populating prices for every open-position underlying). Document this positively in the test docstring.

### Out of scope

* Read-time aggregation into `CashLedger.regt_excess_trailing_*` — story 06b.
* Production wiring of `MarketInputs` from the broker adapter — coordination with the continuous-monitor work tree ([ALP-123](<https://linear.app/alphamind-jatassi/issue/ALP-123>)).

## Acceptance criteria

- [ ] `process_unprocessed_fills` has a required `market_inputs: MarketInputs` keyword parameter.
- [ ] Each processed fill's `fill_records.regt_attribution_json` column is non-null and contains a serialized `RegTMarginAttribution`.
- [ ] Quarantined fills retain `regt_attribution_json IS NULL`.
- [ ] Batched fills are deterministic: fill 2's `regt_margin_before` equals fill 1's `regt_margin_after` for the same underlying.
- [ ] Every existing test in the named list compiles and passes after the migration to threaded `MarketInputs` fixtures.
- [ ] `verify_state_persistence.py` constructs a representative `MarketInputs` and runs cleanly end-to-end.
- [ ] New tests in `tests/execution/state_persistence/test_phase1_regt_attribution.py` cover the five cases in Scope §5.
- [ ] No new I/O at the math layer (the wedge belongs in `phase1.py`, not in the attribution package).
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` and `uv run pytest tests/execution/regt_margin_attribution/ -n auto` both pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` clean.

## Verification

Run `uv run pytest tests/execution/state_persistence/ -n auto` and confirm the new attribution tests pass alongside the migrated existing suite. Spot-check the wedge in `phase1.py` to confirm: (a) attribution runs only for `FillRecord` events (not CA activities); (b) `_read_all_positions` is called twice per fill; (c) the existing `_mark_processed` call is unchanged in semantics.