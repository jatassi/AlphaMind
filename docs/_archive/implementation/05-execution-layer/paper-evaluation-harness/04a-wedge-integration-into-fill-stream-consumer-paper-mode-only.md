# 04a — Wedge integration into fill_stream_consumer (paper-mode only)

## Goal

Wires `compute_live_execution_estimate` into the continuous monitor's `fill_stream_consumer` task so paper-mode fills land in `fill_records` with the `live_execution_estimate_json` column populated. Live-mode fills bypass the harness entirely (column stays NULL). The wedge sits between `fill_report_to_fill_record` translation and `append_fill_record` persistence — per the `FillReport` docstring intent at `fill_stream.py:65` and parent decision (B).

The wedge looks up each fill's harness inputs (order_type from the `orders` table, ADV from the distillation repo, realized volatility from the existing per-underlying vol map, underlying price from the live cache), calls the harness composition, and attaches the estimate via `FillRecord` `model_copy`.

## Reading

* `docs/design/05-execution-layer/paper-evaluation-harness.md` § Activation and deactivation — paper-only gate framing
* `docs/design/05-execution-layer/architecture.md` § Fill report contract — clarifies that `execution_venue` is `paper` or absent in paper mode; the wedge does not need to inspect this field, gating happens at construction
* `src/alphamind/execution/broker_adapter/fill_stream.py:57` — `FillReport` docstring confirming the wedge belongs in the monitor's run-loop
* `src/alphamind/execution/continuous_monitor/fill_stream_consumer/task.py` — current `run_fill_stream_consumer` + `_persist_one` to extend
* `src/alphamind/execution/continuous_monitor/fill_stream_consumer/translation.py` — current `fill_report_to_fill_record` (writes `live_execution_estimate=None`)
* `src/alphamind/state/records.py:75` — `FillRecord` Pydantic shape with the field to populate
* `src/alphamind/portfolio_state/records/positions.py:90` — `LiveExecutionEstimate` typed shape
* `src/alphamind/portfolio_state/records/orders.py` — `OrderRecord` shape with `order_type` (market/limit/stop) and `side`; the table is `orders` keyed by `order_id`
* `src/alphamind/distillation/_repository.py` § `load_ticker_adv` — ADV lookup by ticker
* `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py` § `RealizedVolEntry` — per-underlying realized-vol scalar; the wedge reads from a `Mapping[str, RealizedVolEntry]`
* `src/alphamind/execution/continuous_monitor/underlying_stream/cache.py` — `UnderlyingPriceCache` for current price (not strictly needed if the FillReport.fill_price is sufficient — see scope below)
* `src/alphamind/config/models/main.py` — `ExecutionMode` enum threaded into the wedge construction site
* `src/alphamind/execution/paper_evaluation_harness/harness.py` — story 03's `compute_live_execution_estimate`

## Depends on

* `ALP-527` (story 03) — provides `compute_live_execution_estimate`.

## Scope

Two new surfaces + one modification to the consumer task. All under `src/alphamind/execution/paper_evaluation_harness/` (the enrichment callable) and `src/alphamind/execution/continuous_monitor/fill_stream_consumer/` (the task wiring). Tests at `tests/execution/paper_evaluation_harness/test_enrichment.py` and `tests/execution/continuous_monitor/fill_stream_consumer/test_task.py` (extend existing).

### 1\. New `enrichment.py` module in `paper_evaluation_harness/`

New module `src/alphamind/execution/paper_evaluation_harness/enrichment.py` with:

```python
async def attach_live_execution_estimate(
    record: FillRecord,
    *,
    order_lookup: OrderLookup,
    adv_lookup: AdvLookup,
    vol_lookup: VolLookup,
    config: PaperHarness,
) -> FillRecord: ...
```

Where:

* `OrderLookup` is a Protocol: `def get_order_attributes(order_id: str) -> OrderAttributes | None` returning `OrderAttributes(order_type: OrderType, side: Literal["buy","sell"], instrument_type: InstrumentType, ticker_or_underlying: str)`. Production impl reads from the `orders` table.
* `AdvLookup` is a Protocol: `def get_adv_shares(ticker: str) -> float | None`. Production impl wraps the distillation repository's `load_ticker_adv` (returns `avg_daily_volume_shares` or `None`).
* `VolLookup` is a Protocol: `def get_realized_volatility(underlying: str) -> float | None`. Production impl reads from the `Mapping[str, RealizedVolEntry]` already constructed for `FixtureIvProvider` in `phase1_inputs.py` — but **note** the production map is currently empty (see § Surfacing). For now, the wedge handles `None` gracefully: when vol is unavailable, the harness returns `None` and the FillRecord persists with `live_execution_estimate=None`.

Logic:

* Look up order attributes via `order_lookup`. If `None` (order_id not in `orders` — should not happen for a fill that just produced a `FillRecord`), log a WARNING and return the record unchanged.
* Look up ADV via `adv_lookup` (None → wedge passes None to harness → harness returns None).
* Look up vol via `vol_lookup` (same fallback).
* Determine `instrument_type`. For STRATEGY mleg parent fills the translator already returns `None` (per `translation.py:67`) so STRATEGY does NOT reach this wedge; per-leg children carry their leg `instrument_type` (EQUITY or OPTIONS).
* Call `compute_live_execution_estimate` with the looked-up values.
* If the result is None (data unavailable), return the record unchanged.
* If the result is a `LiveExecutionEstimate`, return `record.model_copy(update={"live_execution_estimate": estimate})`.

The function is async because the lookups are async (DB-backed in production). Protocols' methods are async too — declare them with the `async def ... -> ...` shape.

Add `attach_live_execution_estimate` to `paper_evaluation_harness/__init__.py` `__all__`.

### 2\. Production `OrderLookup` / `AdvLookup` / `VolLookup` adapters

New module `src/alphamind/execution/paper_evaluation_harness/lookups.py` carrying the production wiring:

* `SqlOrderLookup` reads from the `orders` table via the existing `OrderRecord` codec.
* `SqlAdvLookup` wraps the existing `DistillationRepository.load_ticker_adv` (or its Protocol-typed equivalent).
* `MapVolLookup` wraps a `Mapping[str, RealizedVolEntry]` — same source as `FixtureIvProvider`.

These adapters' interfaces match the Protocols in module (1). Each is tested in isolation with the in-memory fakes convention from [ALP-454](https://linear.app/alphamind-jatassi/issue/ALP-454/architecture-refactoring-2026-05-12-audit).

### 3\. Modify `fill_stream_consumer/task.py`

Extend `run_fill_stream_consumer` to accept an optional `enrichment_callable: Callable[[FillRecord], Awaitable[FillRecord]] | None = None` parameter:

```python
async def run_fill_stream_consumer(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    session_factory: ...,
    stream_factory: ...,
    trading_client_factory: ...,
    account_state_queries_factory: ...,
    enrichment_callable: Callable[[FillRecord], Awaitable[FillRecord]] | None = None,
) -> None: ...
```

`_persist_one` calls the enrichment when set:

```python
async def _persist_one(report, *, session_factory, enrichment_callable):
    record = fill_report_to_fill_record(report)
    if record is None:
        return
    if enrichment_callable is not None:
        record = await enrichment_callable(record)
    async with session_factory() as db:
        await append_fill_record(db, record)
        await db.commit()
```

When `enrichment_callable=None` (live mode), behavior is unchanged from today. When the callable is set (paper mode), every persisted FillRecord receives the wedge.

`_replay_recovery`'s `_persist_one` calls also pass the callable through — the recovery path must be annotated identically.

### 4\. Modify `continuous_monitor/__main__.py` (or wherever the consumer task is constructed)

At construction time:

```python
if execution_mode is ExecutionMode.paper:
    config_tree = load_config_tree()  # or however the harness config arrives
    paper_harness_config = config_tree.execution.paper_harness
    order_lookup = SqlOrderLookup(session_factory)
    adv_lookup = SqlAdvLookup(distillation_repo)
    vol_lookup = MapVolLookup(realized_vol_map)
    enrichment_callable = partial(
        attach_live_execution_estimate,
        order_lookup=order_lookup,
        adv_lookup=adv_lookup,
        vol_lookup=vol_lookup,
        config=paper_harness_config,
    )
else:
    enrichment_callable = None

await run_fill_stream_consumer(
    session=session,
    config=config,
    ...,
    enrichment_callable=enrichment_callable,
)
```

The `enrichment_callable=None` path keeps live-mode hot path untouched.

### 5\. Tests

* Unit tests for `attach_live_execution_estimate` with in-memory fakes for all three lookup Protocols:
  * Equity buy with all inputs available → record gets `live_execution_estimate` populated, all fields non-negative, `live_adjusted_fill_price > fill_price`.
  * Equity buy with ADV lookup returning None → record passes through unchanged.
  * Equity sell with all inputs → `live_adjusted_fill_price < fill_price`.
  * Options buy with all inputs (volume substrate present) → record populated with options fees.
  * Options buy with volume lookup returning None → record passes through unchanged.
  * Order-not-found → record passes through unchanged + WARNING logged.
* Integration test for `_persist_one`: with `enrichment_callable=None`, behaves as today (existing test). With a fake callable, the record reaches `append_fill_record` with the field populated.

### Out of scope

* P/L aggregate (story 04b)
* Wiring a real production vol source (handled separately; today's empty map → all estimates default to None). Surface this to the orchestrator if a vol source becomes available mid-story.
* Modifying `fill_report_to_fill_record` — translation stays unchanged.

## Acceptance criteria

- [ ] `attach_live_execution_estimate` is importable from `alphamind.execution.paper_evaluation_harness`.
- [ ] Equity buy with all inputs non-None → returned record's `live_execution_estimate.live_adjusted_fill_price > fill_price`.
- [ ] Equity sell with all inputs non-None → `live_adjusted_fill_price < fill_price`.
- [ ] ADV lookup returning None → returned record's `live_execution_estimate is None` (unchanged).
- [ ] Vol lookup returning None → returned record's `live_execution_estimate is None` (unchanged).
- [ ] Order-not-found → returned record unchanged + a WARNING log entry emitted.
- [ ] Options buy with all inputs non-None → returned record's estimate has CAT + ORF + OCC fees only.
- [ ] Options buy with volume lookup returning None → returned record unchanged.
- [ ] `_persist_one` with `enrichment_callable=None` produces the same `FillRecord` shape as today (regression test against existing fixture).
- [ ] `_persist_one` with a fake `enrichment_callable` produces a `FillRecord` reaching `append_fill_record` with `live_execution_estimate` non-None.
- [ ] In `continuous_monitor/__main__.py`, the construction-site branch on `ExecutionMode.paper` correctly attaches the enrichment callable, and the live-mode branch attaches `None`.
- [ ] `SqlOrderLookup`, `SqlAdvLookup`, `MapVolLookup` each have unit tests against in-memory fakes.
- [ ] import-linter passes: `paper_evaluation_harness/` does NOT import from `continuous_monitor/`. The Protocols are defined in `paper_evaluation_harness/`; production adapters under `paper_evaluation_harness/lookups.py` import from `state/` and `distillation/`, not from `continuous_monitor/`.
- [ ] Existing `tests/execution/continuous_monitor/fill_stream_consumer/test_task.py` tests still pass with the new optional parameter.
- [ ] `uv run pytest --testmon -n auto` (full suite — drop `--testmon` for the final verification per CLAUDE.md "Before claiming done").
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all clean.

## Verification

Scoped tests + lint chain. Manual sanity: run `python -m alphamind.scheduler run --debug-e2e` and check `scripts/verify_debug_e2e.py` still passes — paper-mode synthetic fills should now arrive in `fill_records` with non-null `live_execution_estimate_json` where ADV + vol substrates are populated (or null where they're not).

### Surfacing condition

The production `Mapping[str, RealizedVolEntry]` constructed in `src/alphamind/scheduler/phase1_inputs.py:202` is currently empty (`FixtureIvProvider(surface={}, realized_vol={})`). With an empty map, this wedge produces `live_execution_estimate=None` for every fill. If the subagent discovers that a real per-underlying realized-vol source has landed elsewhere in the codebase (e.g., a distillation table or a separate map), surface to the operator so the production map can be wired through to `MapVolLookup`. If no such source exists, the wedge ships with the empty-map default and a future story populates the vol substrate (out of scope here).