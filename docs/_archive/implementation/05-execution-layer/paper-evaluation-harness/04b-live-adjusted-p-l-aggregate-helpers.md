# 04b — Live-adjusted P/L aggregate helpers

## Goal

Adds read-time helpers in `portfolio_state.computations` that compute the live-adjusted P/L drag for a position (and for the portfolio) by summing the per-fill `LiveExecutionEstimate` values persisted in `fill_records.live_execution_estimate_json`. Per the design doc and parent decision (A): the existing raw `PortfolioPnL` typed record and `PositionView` shape are **untouched** — live-adjusted P/L is computed at read time by downstream consumers (feedback loop, future evaluation reports) that care. This story ships the primitives; consumers wire them later.

## Reading

* `docs/design/05-execution-layer/paper-evaluation-harness.md` § Two P/L aggregates downstream — the raw-vs-adjusted framing and the "computed at read time by consumers that care" principle
* `docs/design/01-data-layer/internal/portfolio-state.md` § 2. P/L and performance tracking — current raw P/L surface for the existing field shape this story parallels
* `src/alphamind/portfolio_state/computations/pnl.py` — existing raw `PortfolioPnL` constructor; reference for arithmetic conventions (Decimal-backed Money accumulators)
* `src/alphamind/portfolio_state/snapshot.py` — existing `PortfolioPnL` typed record (parallel shape to be referenced, not modified)
* `src/alphamind/state/records.py:75` — `FillRecord` with the `live_execution_estimate` field this story reads
* `src/alphamind/portfolio_state/records/positions.py:90` — `LiveExecutionEstimate` 4-field shape (final per parent decision A)
* `src/alphamind/_kernel/money.py` — `Money`, `signed_money`, `DECIMAL_ZERO`

## Depends on

* `ALP-527` (story 03) — the `LiveExecutionEstimate` shape that this story sums over is exercised by story 03. Story 04b's tests fixture-write records with hand-rolled non-null `live_execution_estimate` values, so it does not need story 04a to populate the column in production. Once 04a lands, e2e exercises both together.

## Scope

In scope, under `src/alphamind/portfolio_state/computations/live_adjusted_pnl.py` (new). Tests at `tests/portfolio_state/computations/test_live_adjusted_pnl.py`.

### 1\. `compute_position_live_drag` function

```python
def compute_position_live_drag(fills: tuple[FillRecord, ...]) -> Money: ...
```

Sums the cost components from every fill's `live_execution_estimate` across the input tuple. For fills where `live_execution_estimate is None` (live mode or paper-mode-with-missing-data), contribute zero. The returned `Money` represents the total dollar drag the position would have suffered at live execution relative to its raw Alpaca paper fills.

Drag per fill = `estimated_spread_usd + estimated_impact_usd + estimated_regulatory_fees_usd` (all three Money fields summed). Note: do NOT use `live_adjusted_fill_price` directly here — that field is the per-fill price-level signal; for portfolio-level drag aggregation, the three cost components are the additive primitive.

Edge cases:

* Empty input tuple → `money("0")`.
* All fills with `live_execution_estimate=None` → `money("0")`.
* Mixed (some None, some non-None) → sums the non-None contributions only.

### 2\. `compute_portfolio_live_drag` function

```python
def compute_portfolio_live_drag(
    fills_by_position: Mapping[PositionId, tuple[FillRecord, ...]],
) -> Money: ...
```

Sums `compute_position_live_drag` across all positions. Returns a single Money value: the total portfolio-wide live-execution drag observed across the (paper-mode) fill history. Suitable as the "live-adjusted P/L delta" input that downstream consumers subtract from raw realized P/L.

Edge cases:

* Empty mapping → `money("0")`.

### 3\. Re-export

Add `compute_position_live_drag` and `compute_portfolio_live_drag` to `src/alphamind/portfolio_state/computations/__init__.py` `__all__` so consumers can import via `from alphamind.portfolio_state.computations import compute_position_live_drag`.

### 4\. Tests

* Fixture position with 3 equity-buy fills, each carrying a `LiveExecutionEstimate` of `(estimated_spread_usd=money("2"), estimated_impact_usd=money("1"), estimated_regulatory_fees_usd=money("0.50"), live_adjusted_fill_price=price("101"))`. `compute_position_live_drag` returns `money("10.50")`.
* Fixture position with 2 fills having non-None estimates and 1 fill with `live_execution_estimate=None`. The None fill contributes zero; result is the sum of the other two.
* Fixture position with all fills having `live_execution_estimate=None` (e.g., live mode or pre-harness paper). Result is `money("0")`.
* `compute_portfolio_live_drag` across two positions returns the sum.
* Empty inputs return `money("0")`.

### Out of scope

* Reading fills from the DB — caller responsibility (story 04b ships the pure compute; the read path belongs to whichever consumer adopts it, e.g., feedback loop [ALP-131](https://linear.app/alphamind-jatassi/issue/ALP-131/feedback-loop)).
* Modifying `PortfolioPnL`, `PositionView`, `compute_portfolio_pnl`, or any existing snapshot consumer. Per parent decision (A) and the design doc: raw P/L flow is untouched.
* Wiring into `PortfolioStateRepository` — out of scope for this work tree; consumers add their own read paths.
* New typed record (e.g., `LiveAdjustedPnL`) — keep the surface as primitives. Avoids parallel-record maintenance burden until a consumer actually needs the structured shape.

## Acceptance criteria

- [ ] `compute_position_live_drag` is importable from `alphamind.portfolio_state.computations` and returns a non-negative `Money`.
- [ ] `compute_position_live_drag(())` returns `money("0")`.
- [ ] `compute_position_live_drag(fills)` with 3 fills carrying identical `LiveExecutionEstimate(spread=2, impact=1, fees=0.50)` returns `money("10.50")`.
- [ ] `compute_position_live_drag(fills)` with mixed None / non-None estimates sums only the non-None contributions.
- [ ] `compute_position_live_drag(fills)` with all fills having `live_execution_estimate=None` returns `money("0")`.
- [ ] `compute_portfolio_live_drag` is importable from `alphamind.portfolio_state.computations`.
- [ ] `compute_portfolio_live_drag({})` returns `money("0")`.
- [ ] `compute_portfolio_live_drag({pid_a: fills_a, pid_b: fills_b})` returns the sum of the two per-position drags.
- [ ] `git diff` shows NO edits to `src/alphamind/portfolio_state/snapshot.py`, `src/alphamind/portfolio_state/computations/pnl.py`, `src/alphamind/portfolio_state/views/positions.py`, or any other existing raw-P/L surface. The story is purely additive.
- [ ] `tests/portfolio_state/computations/test_live_adjusted_pnl.py` exists and passes under `uv run pytest tests/portfolio_state/computations/test_live_adjusted_pnl.py --testmon -n auto`.
- [ ] `uv run pytest --testmon -n auto` passes; drop `--testmon` for the final run per CLAUDE.md.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all clean.

## Verification

Scoped test file + lint chain. Spot-check via `git diff --stat src/alphamind/portfolio_state/` that only the new `live_adjusted_pnl.py` and the `__init__.py` re-export are touched.