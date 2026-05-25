## Symptom

`make_dispatch_context_provider`'s closure awaits two independent providers per immediate breach: `snapshot_provider()` (built by `make_snapshot_provider`) and `open_positions_provider()` (built by `make_open_positions_view_provider`). Both walk the same `_assemble_for_breach_loop_tick` pipeline end-to-end:

* `_read_latest_invocation_row` (one SQL SELECT)
* `_load_active_risk_parameters_from_row` (one snapshot-file read + JSON parse)
* `build_sql_portfolio_state_repository` (composition)
* `_build_price_provider` (cache snapshot copy)
* `assemble_snapshot` (14+ sequential SQL reads per `assembler.py:439-471`)

So every immediate-breach event triggers \~30 sequential SQL reads instead of \~15. The two passes produce identical assemblies under normal operation (no invocation rolls between the two awaits inside the single-threaded breach loop).

## Root cause

[ALP-507](https://linear.app/alphamind-jatassi/issue/ALP-507/continuous-monitor-cascade-dispatcher-ships-with-empty-iv-provider) added `make_open_positions_view_provider` because `SqlOpenPositionsReader` returns `PositionRecord`, not the `PositionView` instances the cascade dispatcher's downstream selectors need. The cleanest contained fix at the time was to introduce a second provider with the same pipeline, since changing `SnapshotProvider`'s return type to `AssembledSnapshot` would cascade into the breach loop task and tests.

## Scope

Restructure so that the assemble pipeline runs once per immediate breach. Two candidate designs:

**(A) Single AssembledSnapshot provider on the substrate.** Replace `snapshot_provider` and `open_positions_provider` parameters of `make_dispatch_context_provider` with a single `assembled_snapshot_provider: Callable[[], Awaitable[AssembledSnapshot]]`. Derive both `library_snapshot = to_library_snapshot(assembled.snapshot, ...)` and `open_positions = assembled.snapshot.open_positions` inside the closure. `__main__.py` builds the assembled provider once; the breach-loop task's `snapshot_provider` becomes a thin wrapper that calls the assembled provider and translates.

**(B) Per-tick memoization.** Wrap each provider in a cache keyed by `(invocation_id, as_of)` so the second await reuses the first's result. More state-y but contained — no API change.

Option (A) is cleaner.

## Acceptance criteria

* `make_dispatch_context_provider` runs `assemble_snapshot` at most once per immediate-breach event.
* Existing wiring contracts (breach loop task signature, `SnapshotProvider` type alias if retained) remain compatible.
* A regression test (extension of `TestMakeDispatchContextProvider` in `tests/execution/continuous_monitor/breach_loop/test_production_substrate.py`) counts assembly invocations and asserts == 1 per dispatch.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.

## Related

* Surfaced during [ALP-507](https://linear.app/alphamind-jatassi/issue/ALP-507/continuous-monitor-cascade-dispatcher-ships-with-empty-iv-provider)'s code review (PR #69).
* The shared `_assemble_for_breach_loop_tick` helper landed in [ALP-507](https://linear.app/alphamind-jatassi/issue/ALP-507/continuous-monitor-cascade-dispatcher-ships-with-empty-iv-provider) reduces code duplication but does not eliminate the runtime cost.

## Reading

* `src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py` — `make_snapshot_provider`, `make_open_positions_view_provider`, `make_dispatch_context_provider`, shared `_assemble_for_breach_loop_tick`.
* `src/alphamind/portfolio_state/freshness.py:292` — `AssembledSnapshot` definition.
* `src/alphamind/risk_guardrails/library_snapshot.py` — `to_library_snapshot`.