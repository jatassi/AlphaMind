---
status: done
completed_date: 2026-04-27
commit_id: 17c07d8
---

# 06 — Snapshot assembler

## Goal

Implement `assemble_snapshot` — the orchestrator that produces a `PortfolioStateSnapshot` (story 04a) at invocation time. It fetches raw records via the `PortfolioStateRepository` (story 04b), enriches per-position and per-order computed fields using market prices from the `CurrentPriceProvider` (story 03g) and the computation modules (stories 05a–e), and seals the snapshot. One async function plus the small helper types it needs.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — the full feature spec; the assembler is the data layer's "ingestion layer" that the design's category-by-category descriptions reference
- `../../../design/05-execution-layer/state-persistence.md` § Read paths § Invocation snapshot — the read-isolation contract; the snapshot is produced "after Phase 1 commits and before the analysis pipeline runs"
- `../../../design/06-risk-guardrails/state-delivery.md` § Portfolio state ingestion payload — the interface boundary with the risk-guardrails feature: the repository delivers `RiskBudgetConsumption` and `ActiveRiskParameterSet` populated by the enforcement layer; the assembler does not compute these
- `02-package-skeleton-and-config.md` — package layout (`assembler.py` is this story's target) and the `PortfolioStateConfig` model whose values feed the assembler
- `03a-position-records.md` through `03f-thesis-quality-aggregate-records.md` — the typed records the assembler produces / consumes
- `03g-current-price-provider-protocol.md` — `CurrentPriceProvider`, `PriceQuote`
- `04a-master-snapshot.md` — `PortfolioStateSnapshot`, `PortfolioPnL`, `SectorExposureEntry`, `DirectionalExposure`
- `04b-repository-protocol.md` — `PortfolioStateRepository`, `PortfolioPnLInputs`, `CurrentInvocationMetadata`, `PriorInvocationContext`
- `05a-position-computations.md` — per-position computations
- `05b-portfolio-pnl-and-drawdown-rollups.md` — `compute_portfolio_pnl`, `compute_drawdown_by_source_pct`, `compute_total_portfolio_value_usd`
- `05c-sector-and-directional-exposure-rollup.md` — `compute_sector_exposure`, `compute_directional_exposure`, `SectorResolver`
- `05d-capital-order-parameter-computations.md` — capital, order, parameter helpers
- `05e-activity-log-filtering.md` — filtering and projection helpers

## Depends on

- 02
- 03a, 03b, 03c, 03d, 03e, 03f, 03g
- 04a, 04b
- 05a, 05b, 05c, 05d, 05e

## Scope

In scope, all under `src/alphamind/portfolio_state/assembler.py`. Tests at `tests/portfolio_state/test_assembler.py`.

### 1. Public function

```python
async def assemble_snapshot(
    *,
    repository: PortfolioStateRepository,
    price_provider: CurrentPriceProvider,
    sector_resolver: SectorResolver,
    config: PortfolioStateConfig,
    now: datetime,
) -> PortfolioStateSnapshot:
    ...
```

Keyword-only signature; no positional parameters. `now` is supplied by the caller (the pipeline runner) so the assembler is testable without a clock fixture.

### 2. Assembly sequence

The assembler runs these steps in order. Each step's output feeds subsequent steps.

**Step 1 — Fetch invocation metadata and prior context.** Call `repository.get_current_invocation_metadata()` and `repository.get_prior_invocation_context()` concurrently (`asyncio.gather`). Both return small typed records and feed snapshot scaffolding.

**Step 2 — Fetch raw OMS records.** Issue all category-1-through-6 read calls concurrently via `asyncio.gather`:
- `get_open_positions`, `get_pending_positions`
- `get_drawdown_state`, `get_portfolio_pnl_inputs`
- `get_active_theses`, `get_recent_thesis_resolutions(lookback_trading_days=config.thesis_resolutions_lookback_trading_days)`
- `get_cash_ledger`, `get_pending_orders`, `get_risk_budget_consumption`, `get_active_risk_parameters`
- `get_intra_invocation_changelog(invocation_id=metadata.invocation_id)`
- `get_recent_pm_decision_log(sliding_window_invocations=config.pm_decision_log_sliding_window_invocations)`
- `get_thesis_quality_aggregates`

**Step 3 — Fetch brackets for known positions.** Compute `position_ids = tuple(p.position_id for p in open_positions + pending_positions)`. Call `repository.get_brackets_for_positions(position_ids=position_ids)`.

**Step 4 — Fetch position-modification trail.** Call `repository.get_position_modification_trail(position_ids=position_ids)`. Returns the dict keyed by position_id.

**Step 5 — Fetch current prices.** Build the ticker set from open and pending positions:
- For EQUITY positions: `position.equity_details.ticker`.
- For OPTIONS positions: both `position.options_details.underlying_ticker` (used for delta-adjusted exposure) and the option contract identifier (used for option market value). For this story, the option-contract price is *not* fetched; option market value is computed using `premium_paid_per_contract` as a placeholder pending an option-price provider. **This is a known limitation** documented in Notes.
- For STRATEGY positions: each leg's underlying ticker.

Call `price_provider.get_quotes(tickers=tuple(...), freshness_threshold_seconds=config.snapshot_freshness_max_price_age_seconds)` once. The result dict is keyed by ticker; tickers absent from the result are treated as missing-price (handled by Step 6).

**Step 6 — Enrich each position with per-position computed fields (first pass — without weight).** For each `position` in `open_positions + pending_positions`:
- Resolve the position's pricing ticker per the rules in Step 5.
- Look up the price in the result dict. If the ticker is absent, the position is *price-stale*: log a structured warning (via the standard logging module; this is not an exception) and use a `PriceQuote` constructed with `is_stale=True`, `source=PriceSource.STALE_FALLBACK`, `price_usd=0.0` as a sentinel. The position's computed fields will be zero, and downstream staleness reporting (story 08) flags the position.
- Compute via story 05a's functions:
  - `current_market_value_usd` (or `compute_strategy_market_value_usd` for strategies — see Notes)
  - `unrealized_pnl_usd`, `unrealized_pnl_pct`
  - `position_age_hours` (using `now`)
  - `notional_exposure_usd`, `delta_adjusted_exposure_usd`
  - `distance_to_target_usd`, `distance_to_stop_usd`, `risk_reward_at_current` (consults the bracket from Step 3)
- Construct an enriched `PositionRecord` via `position.model_copy(update={...})`.

**Step 7 — Compute total portfolio value.** Use `compute_total_portfolio_value_usd(open_positions=enriched_open, pending_positions=enriched_pending, cash_ledger=cash_ledger)` from story 05b. Pending positions are included in the total per 05b's documented contract.

**Step 8 — Enrich positions with weight (second pass).** For each enriched position from Step 6, call `compute_position_weight_pct(market_value, total_portfolio_value)` and `model_copy` again to set `position_weight_pct`. Re-sort each list by `position_id` ascending for the snapshot's stable-consumption contract.

**Step 9 — Enrich pending orders with age.** For each `OrderRecord` in `pending_orders`, compute `age_hours = compute_order_age_hours(order.submission_timestamp, now)` (story 05d) and `model_copy(update={"age_hours": age_hours})`. Sort by `submission_timestamp` ascending.

**Step 10 — Compute portfolio P/L rollup.** Call `compute_portfolio_pnl(open_positions=enriched_open, inputs=portfolio_pnl_inputs, total_portfolio_value_usd=total_portfolio_value)` from story 05b.

**Step 11 — Enrich `CashLedger` with computed fields.** Compute:
- `cash_pct_of_portfolio = compute_cash_pct_of_portfolio(cash_ledger.current_cash_usd, total_portfolio_value)` (story 05d)
- `true_deployable_capital_usd = compute_true_deployable_capital_usd(cash_ledger)` (story 05d)
The repository-supplied `regt_excess_*_usd` fields pass through unchanged.
Construct an enriched `CashLedger` via `model_copy(update={...})`.

**Step 12 — Enrich `DrawdownState` with `drawdown_by_source_pct` if empty.** If `drawdown_state.drawdown_by_source_pct == {}` and `drawdown_state.current_drawdown_pct > 0`, call `compute_drawdown_by_source_pct(open_positions=enriched_open, current_drawdown_pct=drawdown_state.current_drawdown_pct)` from story 05b and `model_copy(update={"drawdown_by_source_pct": ...})`. Otherwise leave the repository's value intact.

**Step 13 — Compute sector and directional exposure.** Call `compute_sector_exposure(open_positions=enriched_open, resolver=sector_resolver, total_portfolio_value_usd=total_portfolio_value)` and `compute_directional_exposure(open_positions=enriched_open, total_portfolio_value_usd=total_portfolio_value)` from story 05c.

**Step 14 — Compute parameter change flag.** Call `compute_parameter_change_flag(current=active_risk_parameters, prior=prior_invocation_context.prior_active_risk_parameters)` from story 05d. Construct an enriched `ActiveRiskParameterSet` via `model_copy(update={"parameter_change_flag": ...})`.

**Step 15 — Project activity log into category-5 views.** The repository methods already return the per-category projections, but normalize:
- `intra_invocation_changelog` is the repository's `get_intra_invocation_changelog(invocation_id)` result (already filtered).
- `recent_pm_decision_log` is the repository's `get_recent_pm_decision_log(sliding_window_invocations)` result (already filtered to PM_DECISION).
- `position_modification_trail` is the repository's `get_position_modification_trail(position_ids)` result (already keyed by position_id, restricted to known positions).

The story 05e helpers are *available* for downstream consumers but not used here — the repository's methods are the canonical source.

**Step 16 — Construct and return the snapshot.** Build `PortfolioStateSnapshot` with all the enriched records and rollups assembled in Steps 6–15. Set `snapshot_assembled_at = now`. Per `04a`'s validators, the snapshot's structural invariants are checked at construction.

### 3. Helper: ticker resolution

A small private helper `_resolve_pricing_tickers(positions: tuple[PositionRecord, ...]) -> tuple[str, ...]` that walks positions and returns the unique set of tickers needed for pricing per the rules in Step 5. Returns a tuple (ordered for determinism) for use as `price_provider.get_quotes(tickers=...)`.

### 4. Error handling

- `RepositoryReadError` from any repository call propagates without modification. The assembler does not catch infrastructure errors; the pipeline runner is responsible for handling these as invocation aborts per the failure-handling design.
- `RepositoryConsistencyError` propagates similarly — a Phase 1 / Phase 2 isolation violation aborts the invocation.
- A `CurrentPriceProvider` raising `UnknownTickerError` for a *known position's* ticker is treated as in-band staleness (per Step 6): the position is flagged stale, the assembler logs a warning, and assembly continues. This matches the design's intent that the assembler delivers a snapshot even when prices are stale.
- `MissingLegPriceError` from a strategy-position computation propagates as a `ValueError` (the assembler does not have a graceful-recovery path for a strategy with missing leg prices; this is a setup error in the pricing data).

### 5. Tests

Tests at `tests/portfolio_state/test_assembler.py` use `StubPortfolioStateRepository` and `StubCurrentPriceProvider` directly.

**Happy path — empty portfolio.** All repository tuples empty, cash ledger populated, drawdown zero. Verify the assembled snapshot has empty positions/orders/brackets, zero portfolio value, and the PortfolioPnL is all zeros. No exceptions; no warnings logged.

**Happy path — one open equity position.** One LONG NVDA position; price provider returns a fresh quote. Verify the snapshot has one open position with `current_market_value_usd`, `position_weight_pct`, `unrealized_pnl_usd` matching the documented computation. Sector exposure has one entry for the resolved sector. Directional exposure shows net long matching position size.

**Happy path — multi-position portfolio.** Three positions across two sectors, one short, one long-with-bracket-stop, one with no bracket. Verify all per-position fields are correctly enriched, sector rollup has two entries, directional exposure is net long, and `position_modification_trail` is correctly keyed.

**Stale price.** Price provider returns `is_stale=True` for one position; assembler logs the warning and continues; the snapshot's affected position has `current_market_value_usd = 0.0` (per the sentinel quote) but the snapshot still constructs successfully.

**Unknown ticker (missing from price provider's result).** `get_quotes` omits the ticker; assembler treats as stale; same handling as above.

**Repository read error.** A test stub repository raises `RepositoryReadError` from one method; the exception propagates from `assemble_snapshot` without being swallowed.

**Parameter change flag.** Two scenarios: (a) prior context is `None` → flag is `False`; (b) prior parameters differ → flag is `True`. Verify the assembled `ActiveRiskParameterSet.parameter_change_flag` matches.

**Drawdown-by-source enrichment.** Repository returns a `DrawdownState` with `current_drawdown_pct = 5.0` and `drawdown_by_source_pct = {}`; the assembler enriches the dict using positions' P/L. A second test where the repository returns `drawdown_by_source_pct = {"POS-1": 3.0, "POS-2": 2.0}` already populated; the assembler does not overwrite.

**Determinism.** Identical inputs (repository fixture, price-provider fixture, sector resolver, config, `now`) produce identical snapshots across repeated calls (snapshot equality via `==` on the frozen Pydantic model).

**Two-pass weight enrichment correctness.** Construct a fixture with two positions of known market values and a known cash balance; verify each position's `position_weight_pct` matches the documented formula `(abs(market_value) / total_portfolio_value) * 100`.

**Pending order enrichment.** Repository returns a pending order with `age_hours = 0.0` placeholder; assembler enriches to actual age based on `now - submission_timestamp`. Verify the resulting value.

**Strategy position with missing leg price.** Construct a strategy fixture with a leg whose underlying ticker is missing from the price-provider result; verify the assembler raises (or in-band-handles) per the documented contract — `MissingLegPriceError` propagates.

Out of scope:
- Production wiring against a real OMS or live price feed (future stories).
- Caching across invocations (each invocation produces a fresh snapshot).
- Concurrent invocations (the assembler is invoked once per invocation; multi-invocation concurrency is the pipeline's concern).
- Logging configuration (uses the standard `logging` module; the pipeline configures handlers).
- Failure-policy escalation (deferred to the pipeline runner; the assembler raises and the runner decides).
- Per-consumer view projection (story 07; the assembler produces the master snapshot, projections are downstream).

## Notes

The two-pass enrichment for `position_weight_pct` (Step 6 then Step 8) reflects the genuine dependency: per-position market value can be computed independently, but weight requires the portfolio-level total. Doing it in two passes is the simplest implementation; combining into one pass would require materializing a list of (position, market_value) pairs and a final reconstruction. Two passes are clearer.

**Option pricing limitation (Step 5 / 6).** The pricing in this assembler covers underlying prices (used for delta-adjusted exposure and equity market value). Option premium prices (used for the option's *own* market value) require a separate fetch path against a contract-snapshot store like `OptionsContractSnapshots` in `src/alphamind/persistence/models.py`. Story 06 uses each options position's `premium_paid_per_contract` (the at-entry value) as the option's mark-to-market price. This is **a known imprecision**: when the option's premium has moved, `current_market_value_usd` lags by the premium drift. A future story can introduce an `OptionPriceProvider` Protocol and refine this. The unrealized P/L on options is correspondingly approximate. This is documented in the assembler's docstring and surfaced via a stub-comment field on `PositionRecord` if needed; for the scope of this story, the limitation is accepted.

**Strategy positions** require per-leg underlying prices (for delta-adjusted exposure) and per-leg option prices (for market value). The same option-pricing limitation applies; per-leg `premium_paid_per_contract` is used as a stand-in. The leg-price dict passed to `compute_strategy_*_usd` functions uses the per-leg underlying ticker.

**Sector resolution** uses `sector_resolver`, an injected callable. The assembler does not own the resolver implementation — when the production sector-classification reader lands, it implements the `SectorResolver` Callable shape. For tests, a simple closure returning sector labels per ticker is sufficient.

**Activity log filtering**: the repository methods return per-category projections directly, so the assembler does not call story 05e helpers. Story 05e exists for downstream consumers (the per-consumer view projections in story 07) that need ad-hoc slicing.

Per `feedback_simplify_before_building.md`, `assemble_snapshot` is one async function. No `Assembler` class; no builder; no pipeline-stage abstraction. The 16-step sequence is documented in the function's docstring.

Per `feedback_no_inventing_component_names.md`, the function name `assemble_snapshot` describes what it produces; the noun-form `assembler` matches the module name in story 02's package layout.

The `now` parameter is supplied by the caller and threaded through `compute_position_age_hours` and `compute_order_age_hours`. Tests verify behavior across different `now` values without freezing system time.

The assembler is async because the repository and price-provider methods are async. Internally it uses `asyncio.gather` for the parallel fetches in Steps 1–4 to minimize wall-clock time. CPU-bound enrichment in Steps 6–14 is straight-line synchronous.

Per the design's "snapshot read see a consistent state: all Phase 1 updates, no Phase 2 updates" (state-persistence.md § Snapshot isolation), the assembler trusts the repository's isolation guarantees. It does not implement transaction boundaries itself.

`RepositoryConsistencyError` propagation matches AlphaMind's "any agent failure aborts the invocation" memory: an isolation violation is a fail-loud condition that halts the pipeline rather than being silently corrected.

## Acceptance criteria

- [ ] `assemble_snapshot(*, repository, price_provider, sector_resolver, config, now)` is declared with the documented signature and is an async function.
- [ ] The 16-step assembly sequence is implemented in the documented order.
- [ ] Empty-portfolio happy path returns a valid `PortfolioStateSnapshot` with all-empty tuples and zero rollups.
- [ ] Single-equity-position happy path produces correct `current_market_value_usd`, `position_weight_pct`, `unrealized_pnl_usd`, sector exposure, and directional exposure.
- [ ] Multi-position happy path produces correct rollups across multiple positions and multiple sectors.
- [ ] Two-pass weight enrichment correctness: `position_weight_pct` matches `(abs(market_value) / total_portfolio_value) * 100` across a multi-position fixture.
- [ ] Stale price (in-band `is_stale=True` from price provider) does not abort assembly; the affected position has `current_market_value_usd == 0.0` and the snapshot constructs.
- [ ] Unknown ticker missing from `get_quotes` result is handled as staleness (warning logged, sentinel quote used, assembly continues).
- [ ] `RepositoryReadError` raised by any repository call propagates from `assemble_snapshot` without modification.
- [ ] `RepositoryConsistencyError` propagates similarly.
- [ ] `parameter_change_flag` is `False` when prior context is `None`; `True` when prior parameters differ.
- [ ] `drawdown_by_source_pct` is enriched when the repository returns it empty and `current_drawdown_pct > 0`.
- [ ] `drawdown_by_source_pct` is left intact when the repository returns it pre-populated.
- [ ] Pending orders' `age_hours` is enriched from `now - submission_timestamp`.
- [ ] Strategy position with missing leg price raises `MissingLegPriceError` (or its `KeyError` superclass) from `assemble_snapshot`.
- [ ] Determinism: identical inputs produce identical snapshots across repeated calls.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
