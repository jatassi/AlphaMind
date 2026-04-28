---
status: not_started
completed_date:
commit_id:
---

# 09 — End-to-end verification

## Goal

Land a comprehensive integration-test suite at `tests/portfolio_state/test_end_to_end.py` that exercises the full Portfolio state path against the stub repository (story 04b), stub price provider (story 03g), and stub sector resolver — from `assemble_snapshot` (story 06) through `compute_snapshot_freshness` (story 08) to all four per-consumer view projections (story 07). The suite is the final gate that a story-set implementation works end-to-end before the work tree is declared done.

The tests use only the in-tree stubs and Pydantic models — no production database, no broker connection, no live market data. They verify that the typed records, computations, assembler, freshness, and consumer views compose correctly under realistic inputs.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — the feature spec; the tests verify each category 1–6 surfaces correctly through the snapshot
- `02-package-skeleton-and-config.md` — package and test layout
- All stories 03a–08 — the test suite exercises every type, computation, and projection they declare
- `../../../implementation/03-analysis-layer/synthesizer/11-end-to-end-verification.md` (if present) and `../../../implementation/02-distillation-layer/13-end-to-end-verification.md` (if present) — sibling end-to-end-verification stories for structural pattern

## Depends on

- 02 through 08 (every prior story in this work tree)

## Scope

In scope, all under `tests/portfolio_state/test_end_to_end.py`. Tests are organized into three sections matching the test scenarios; each section is its own pytest test class for clean output.

### 1. Fixture builder

A module-scope fixture builder (`tests/portfolio_state/_fixtures.py`) encapsulating the stub-construction logic so tests don't duplicate boilerplate. Functions:

- `build_minimal_snapshot_inputs() -> tuple[RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime]`
  - Empty portfolio: zero positions, zero theses, zero pending orders, populated cash ledger ($100,000 settled), zero drawdown, default risk parameters.
- `build_multi_position_snapshot_inputs() -> tuple[RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime]`
  - Three open equity positions: NVDA long ($50,000 cost, 100 shares), AMD short ($30,000 cost, 250 shares), JPM long ($20,000 cost, 100 shares). One pending equity entry for AAPL ($15,000, 50 shares). One active thesis per open position. Brackets covering each. Cash ledger with $50,000 settled cash. A few activity-log entries for `intra_invocation_changelog` and `position_modification_trail`. Sector resolver mapping NVDA/AMD → "tech", JPM → "financials", AAPL → "tech". Price quotes: all fresh, NVDA at $510 (up), AMD at $115 (down — short profit), JPM at $195 (down — long loss), AAPL at $185 (above pending limit).
- `build_options_position_snapshot_inputs() -> tuple[RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime]`
  - One long call option position on NVDA, with greeks set per the design.
  - One long put option on TSLA (negative delta — a `Direction.LONG` position whose delta-adjusted exposure is negative; tests the directional-bucketing behavior in story 05c).
- `build_stale_pricing_snapshot_inputs() -> tuple[RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime]`
  - Two open positions; the price provider's quotes are 60 minutes old (older than the 15-minute `max_price_age_seconds`). Verifies the assembler's stale-price handling.
- `build_unknown_ticker_snapshot_inputs() -> tuple[RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime]`
  - Two open positions; the price provider's `get_quotes` result omits one of the tickers entirely. Verifies the assembler's unknown-ticker fallback.

Each helper returns the tuple ready to construct `StubPortfolioStateRepository`, `StubCurrentPriceProvider`, the resolver closure, the config, and the `now` timestamp.

### 2. Section A — Smoke tests against the stubs

For each fixture builder above, run a smoke test:
- Construct the stub repository and stub price provider.
- Call `await assemble_snapshot(repository=..., price_provider=..., sector_resolver=..., config=..., now=...)`.
- Verify `AssembledSnapshot` is returned (not raised).
- Verify the snapshot's all structural validators pass (i.e., the snapshot constructs without `ValidationError`).
- Verify the freshness sidecar is populated with sensible values (e.g., for the empty-portfolio fixture, `total_positions == 0`, `oldest_price_age_seconds is None`).

### 3. Section B — Per-consumer projection consistency

Using the `build_multi_position_snapshot_inputs` fixture:
- Project all four consumer views: `project_synthesizer_view(snapshot, sector_resolver=...)`, `project_analyst_view(snapshot, ...)`, `project_strategist_view(snapshot)`, `project_portfolio_manager_view(snapshot)`.
- Verify cross-view consistency:
  - The set of `position_id`s referenced is identical across the views (synthesizer's positions, analyst's held positions, strategist's positions, PM's positions).
  - The synthesizer's `sector_exposure_pct` matches the magnitudes of the strategist's `sector_exposure` per-sector net (long − short).
  - The synthesizer's `net_directional_pct` and `gross_exposure_pct` match the strategist's `directional_exposure.net_directional_pct_of_portfolio` and `gross_pct_of_portfolio`.
  - The PM's `position_modification_trail` dict has exactly the position_ids that have non-empty modification trails in the snapshot.
- Verify per-consumer information hiding:
  - The synthesizer's view does NOT expose P/L, drawdown, full thesis components, or activity log entries (the slim contract).
  - The analyst's `held_positions` entries do NOT expose unrealized P/L or thesis content (dedup-only context).
  - The strategist's view DOES include full thesis components (`StrategistPositionView.thesis.components` non-empty for at least one position).
  - The PM's view includes the strategist's fields PLUS `thesis_quality_aggregates`.

### 4. Section C — Assembler correctness against known formulas

Using `build_multi_position_snapshot_inputs` (where prices and quantities are deterministic):
- Verify each position's `current_market_value_usd` matches `quantity × price` (with the LONG/SHORT sign convention from story 05a).
- Verify each position's `position_weight_pct` matches `(abs(market_value) / total_portfolio_value) * 100` to within floating-point tolerance.
- Verify each position's `unrealized_pnl_usd` matches the documented LONG vs SHORT formula.
- Verify the snapshot's `directional_exposure.gross_pct_of_portfolio` equals the sum of long-bucket plus short-bucket totals divided by total portfolio value × 100.
- Verify `portfolio_pnl.total_unrealized_pnl_usd` equals the sum of per-position `unrealized_pnl_usd`.
- Verify `cash_ledger.cash_pct_of_portfolio` equals `(current_cash / total_portfolio_value) * 100`.
- Verify `cash_ledger.true_deployable_capital_usd` equals `settled - reserved - margin`.

### 5. Section D — Freshness reporting

Using `build_stale_pricing_snapshot_inputs` and `build_unknown_ticker_snapshot_inputs`:
- For the stale fixture: assemble the snapshot. Verify `freshness.position_ids_priced_stale` contains both position_ids; `count_priced_fresh == 0`; `all_position_prices_fresh == False`; `freshness.is_position_price_stale(position_id)` returns `True` for both.
- For the unknown-ticker fixture: assemble. Verify `freshness.position_ids_unknown_ticker` contains the affected position_id; the sentinel `STALE_FALLBACK` was used for that position; `freshness.is_position_price_stale(position_id) == True`.
- For the all-fresh `build_multi_position_snapshot_inputs`: `freshness.all_position_prices_fresh == True`; `freshness.count_priced_stale == 0 and freshness.count_unknown_ticker == 0`.
- Phase 1 → snapshot timing: with the fixture's `now` set 2 seconds after `phase1_committed_at`, verify `freshness.phase1_to_snapshot_seconds == 2.0` and `phase1_to_snapshot_within_threshold == True`.
- Boundary: with `now` set exactly at `max_phase1_to_snapshot_seconds` after `phase1_committed_at`, verify `phase1_to_snapshot_within_threshold == True`. With `now` set 1ms past, verify `False`.

### 6. Section E — Determinism

Using `build_multi_position_snapshot_inputs`:
- Call `assemble_snapshot(...)` twice with identical inputs. Verify the two `AssembledSnapshot` objects compare equal via `==` (frozen Pydantic equality is structural).
- Project all four consumer views from each snapshot and verify pairwise equality.
- Verify identity: identical inputs ⇒ identical outputs across the entire pipeline.

### 7. Section F — Empty / degenerate edge cases

Using `build_minimal_snapshot_inputs`:
- All four projections succeed with empty / zero-valued result fields.
- `sector_exposure` is an empty tuple.
- `directional_exposure.gross_pct_of_portfolio == 0.0`.
- `portfolio_pnl.total_unrealized_pnl_usd == 0.0`; rolling P/L all zero.
- `freshness.total_positions == 0`; `oldest_price_age_seconds is None`.

Cash-only edge case: zero positions, $100,000 cash. Total portfolio value is $100,000 (cash). Cash percentage of portfolio is `100.0`. No exception.

### 8. Section G — Repository error propagation

A small custom failure stub (defined inline in the test file, not in `_fixtures.py`):

```python
class _RaisingRepository:
    async def get_open_positions(self):
        raise RepositoryReadError("simulated DB error")
    # ... other methods raise NotImplementedError
```

Verify that calling `assemble_snapshot` with this repository propagates `RepositoryReadError` without modification.

A second variant raises `RepositoryConsistencyError`; verify it propagates.

### 9. Test execution

- All tests must pass with `uv run pytest tests/portfolio_state/test_end_to_end.py -v`.
- The full test run is fast (<5 seconds total) — no I/O, no networking, no fixtures requiring tokens.
- The suite has at minimum 30 distinct test methods spread across the seven sections.

Out of scope:
- Tests against a real OMS database (the production reader's tests, when it lands).
- Tests against a real broker / Alpaca (the broker-adapter tests).
- Performance benchmarks (the suite verifies correctness, not throughput).
- Soak tests (long-running stability — covered by paper-trading once that lands).
- Tests of the synthesizer / analyst / strategist / PM input-bundle assembly stages — those are downstream consumer concerns, not the data-layer's verification.
- Tests of options-contract pricing (the assembler's known limitation per story 06; deferred until an option-price provider lands).

## Notes

The end-to-end suite is the final integration gate for this work tree. It does not exercise infrastructure (DB, broker, network) — those are tested in their respective work trees with appropriate scaffolding. Here, the contract is: given typed inputs through the stubs, the full assembly + projection + freshness pipeline produces typed outputs that consumers can rely on.

Per `feedback_simplify_before_building.md`, the test suite is straight-line: pytest test functions with explicit asserts, no parametrize-based DRY abstractions until at least three duplicated patterns emerge.

The fixture builders in `_fixtures.py` are the only abstraction. Each builder returns a tuple of all the inputs `assemble_snapshot` needs, ready for stub construction. This keeps the test bodies focused on the assertion semantics, not on fixture setup.

Per `feedback_no_inventing_component_names.md`, fixture and test names mirror the design's vocabulary (`multi_position`, `options_position`, `stale_pricing`, `unknown_ticker`).

The "30 distinct test methods" target is a structural anchor — not a hard cap, not a numeric anchor for behavior. The actual test count emerges from the section's coverage requirements. Per `feedback_avoid_numeric_anchors.md`, the count is documented for orchestrator visibility, not as a quality threshold.

The "<5 seconds" target reflects the design's intent that integration tests run as part of the regular `uv run pytest` flow. Slower tests would discourage frequent runs. If a future fixture pushes the suite past this, the implementer flags it for review before merging.

## Acceptance criteria

- [ ] `tests/portfolio_state/_fixtures.py` provides the five documented fixture-builder functions; each returns the documented tuple shape.
- [ ] `tests/portfolio_state/test_end_to_end.py` covers Sections A through G with at least 30 distinct test methods.
- [ ] Section A: smoke tests against each of the five fixture builders pass.
- [ ] Section B: cross-consumer projection consistency invariants hold on the multi-position fixture.
- [ ] Section B: per-consumer information-hiding invariants verified (synthesizer slim, analyst dedup-only, strategist full, PM strategist+aggregates).
- [ ] Section C: assembler correctness invariants verified against known formulas (market value, weight, P/L, exposure, cash percentages).
- [ ] Section D: stale pricing fixture produces `position_ids_priced_stale` populated; unknown-ticker fixture produces `position_ids_unknown_ticker` populated; all-fresh fixture has `all_position_prices_fresh == True`.
- [ ] Section D: phase1-to-snapshot boundary tests pass at the threshold and 1ms beyond.
- [ ] Section E: identical inputs produce identical `AssembledSnapshot` and identical consumer views.
- [ ] Section F: minimal / empty fixture produces empty-but-valid snapshot and consumer views without exception.
- [ ] Section G: `RepositoryReadError` and `RepositoryConsistencyError` from a failure stub propagate from `assemble_snapshot`.
- [ ] Full test suite runs in `<5` seconds locally.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
