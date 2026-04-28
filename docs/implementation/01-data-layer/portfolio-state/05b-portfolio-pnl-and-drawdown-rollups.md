---
status: in_progress
completed_date:
commit_id:
---

# 05b — Portfolio P/L and drawdown rollups

## Goal

Implement the pure-function rollup that produces a `PortfolioPnL` value (story 04a) from `PortfolioPnLInputs` (story 04b) plus the open positions. Plus a helper that computes per-position drawdown contributions (`drawdown_by_source_pct` on `DrawdownState`, story 03d) when the repository does not pre-populate them. Both helpers operate on already-enriched `PositionRecord` instances; this story does not re-fetch market data.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` § 2b (portfolio-level P/L), § 2c (drawdown tracking) — the consumer-facing rollups this story produces
- `../../../design/05-execution-layer/state-persistence.md` § Tier 3 — Derived/aggregate entities — describes the OMS-side `portfolio_summary` aggregate; this story's rollup mirrors a subset of that aggregate at snapshot time without depending on the OMS aggregate's existence
- `02-package-skeleton-and-config.md` — package layout (`computations/pnl.py` is this story's target)
- `03a-position-records.md` — `PositionRecord.unrealized_pnl_usd`, `current_market_value_usd`, `realized_pnl_to_date_usd`, `position_id`, `equity_details.ticker` (or strategy/options analogues)
- `03d-capital-state-records.md` — `DrawdownState`, `CashLedger`
- `04a-master-snapshot.md` — `PortfolioPnL` (the target type; defined in `snapshot.py`)
- `04b-repository-protocol.md` — `PortfolioPnLInputs` (the OMS-aggregate input)
- `05a-position-computations.md` — sibling pattern: pure functions, structured tests, no I/O

## Depends on

- 02
- 03a (consumes `PositionRecord`)
- 03d (consumes `DrawdownState`, `CashLedger`)
- 04a (target type `PortfolioPnL` lives in `snapshot.py`)
- 04b (consumes `PortfolioPnLInputs`)

## Scope

In scope, all under `src/alphamind/portfolio_state/computations/pnl.py` — pure functions with no I/O. Tests at `tests/portfolio_state/computations/test_pnl.py`.

### 1. Portfolio P/L rollup

`compute_portfolio_pnl(open_positions: tuple[PositionRecord, ...], inputs: PortfolioPnLInputs, total_portfolio_value_usd: float) -> PortfolioPnL`

Behavior:
- `total_unrealized_pnl_usd = sum(p.unrealized_pnl_usd for p in open_positions)` — relies on position-level computations (story 05a) having already populated each `PositionRecord.unrealized_pnl_usd`. If any position has `unrealized_pnl_usd is None` (unenriched), the function raises `ValueError` with a message naming the offending position_id — the assembler's contract is to enrich before rollup.
- `total_unrealized_pnl_pct_of_portfolio = (total_unrealized_pnl_usd / total_portfolio_value_usd) * 100.0` — returns `0.0` when `total_portfolio_value_usd == 0.0` (empty portfolio).
- `daily_realized_pnl_usd = inputs.daily_realized_pnl_usd` — passed through.
- `daily_total_pnl_usd = inputs.daily_realized_pnl_usd + total_unrealized_pnl_usd` — derived.
- `cumulative_realized_pnl_usd = inputs.cumulative_realized_pnl_usd` — passed through.
- `rolling_realized_pnl: dict[Literal["1d","3d","5d","20d"], float]` — built by reading `inputs.rolling_realized_pnl[k]` for each of the four required keys; missing key raises `KeyError` with a message naming the absent key (the OMS aggregate must produce all four).
- `win_rate_pct = inputs.win_rate_pct` — passed through (`None` when no resolved theses).
- `average_win_size_usd = inputs.average_win_size_usd` — passed through.
- `average_loss_size_usd = inputs.average_loss_size_usd` — passed through.
- `profit_factor = inputs.profit_factor` — passed through.

Validation at function entry:
- `total_portfolio_value_usd >= 0` — negative raises `ValueError`.
- All four required `rolling_realized_pnl` keys are present in `inputs.rolling_realized_pnl` — else raise `KeyError`.

### 2. Drawdown-by-source contributions

`compute_drawdown_by_source_pct(open_positions: tuple[PositionRecord, ...], current_drawdown_pct: float) -> dict[str, float]`

When `current_drawdown_pct == 0.0`, returns `{}` (no drawdown ⇒ no per-source attribution).

Otherwise, for each `position` in `open_positions` with `unrealized_pnl_usd < 0` (positions contributing to the drawdown):
- The key is the position's `position_id`.
- The value is the position's *share* of total negative unrealized P/L, scaled to the current drawdown percentage:
  - Let `total_negative = sum(-p.unrealized_pnl_usd for p in open_positions if p.unrealized_pnl_usd < 0)` (a non-negative dollar magnitude).
  - When `total_negative == 0.0`, returns `{}`.
  - Otherwise, the position's share is `(-p.unrealized_pnl_usd / total_negative) * current_drawdown_pct`.
- Positions with non-negative unrealized P/L are omitted from the result (they did not contribute to drawdown).

Validation:
- `current_drawdown_pct >= 0` — negative raises `ValueError` (drawdown is a non-negative magnitude per `03d` `DrawdownState`).
- Each position's `unrealized_pnl_usd` must be non-`None` — else raise `ValueError` with the position_id.

### 3. Total-portfolio-value helper

`compute_total_portfolio_value_usd(open_positions: tuple[PositionRecord, ...], pending_positions: tuple[PositionRecord, ...], cash_ledger: CashLedger) -> float`

- Returns `cash_ledger.current_cash_usd + sum(abs(p.current_market_value_usd) for p in open_positions) + sum(abs(p.current_market_value_usd) for p in pending_positions)`.
- Pending positions are included because their reserved capital is still in `cash_ledger.reserved_capital_usd`; using `abs()` avoids double-subtracting short market value (which is signed negative per story 05a).
- Validation: every position's `current_market_value_usd` must be non-`None`; else raise `ValueError` with the position_id.

This helper exists so the assembler can call it once and pass the result into `compute_portfolio_pnl` and `compute_position_weight_pct` (story 05a) — keeping a single source of truth for "total portfolio value" within one snapshot.

### 4. Tests

Per function, pure-input → pure-output tests:

- `compute_portfolio_pnl` happy path: build a fixture with two open positions (one positive, one negative `unrealized_pnl_usd`), a `PortfolioPnLInputs` with all keys and finite values, and a known `total_portfolio_value_usd`. Verify each output field matches the documented formula.
- `compute_portfolio_pnl` unenriched position: a position with `unrealized_pnl_usd = None` raises `ValueError` naming the position_id.
- `compute_portfolio_pnl` empty portfolio: `open_positions = ()`, `total_portfolio_value_usd = 0.0`, `inputs` with zeros → returns a `PortfolioPnL` with all zero rollups; `total_unrealized_pnl_pct_of_portfolio == 0.0` (no zero-division exception).
- `compute_portfolio_pnl` missing rolling key: omit `"5d"` from `inputs.rolling_realized_pnl` → `KeyError` naming `"5d"`.
- `compute_portfolio_pnl` negative `total_portfolio_value_usd` → `ValueError`.
- `compute_drawdown_by_source_pct` happy path: three open positions, two with negative P/L, one positive; `current_drawdown_pct = 4.0`. Verify the returned dict has exactly the two negative-P/L position_ids as keys and the values are the documented proportional split summing to `4.0` within tolerance.
- `compute_drawdown_by_source_pct(positions, 0.0)` returns `{}`.
- `compute_drawdown_by_source_pct` with no negative-P/L positions returns `{}`.
- `compute_drawdown_by_source_pct` with `current_drawdown_pct < 0` raises `ValueError`.
- `compute_drawdown_by_source_pct` with a position whose `unrealized_pnl_usd is None` raises `ValueError` naming the position_id.
- `compute_total_portfolio_value_usd` happy path: cash + signed market values (using `abs()`) → matches the documented sum.
- `compute_total_portfolio_value_usd` with a position whose `current_market_value_usd is None` raises `ValueError` naming the position_id.
- All functions: identical inputs produce identical outputs across repeated calls (parametrized determinism test).

Out of scope:
- Position-level P/L (story 05a — already populated on each `PositionRecord` before this story's rollup runs).
- Drawdown high-water-mark tracking (OMS write-side; the read-side `DrawdownState` is delivered by the repository).
- Per-sector drawdown attribution — the function emits per-position contributions; sector aggregation is the consumer's concern (or a follow-up if a per-sector dimension is needed).
- Rolling-window arithmetic (1d/3d/5d/20d realized) — the OMS aggregate produces these; this story passes them through.
- P/L attribution decomposition into market / sector / alpha components (distillation-layer derived metrics 8a/8b — separate work tree).
- Weighted average correlation, dependency clustering — derived metrics 9a/9b — separate work tree.

## Notes

`compute_portfolio_pnl` is a thin transformation: most fields pass through from `PortfolioPnLInputs`, and the only computed value is `total_unrealized_pnl_usd` (and the percentage-of-portfolio derived from it). The function exists primarily to produce a single typed `PortfolioPnL` from the two upstream inputs, with the validation that every field on the target type is populated.

`compute_drawdown_by_source_pct` is offered because the design (`portfolio-state.md` § 2c) names "drawdown by source" as a delivered field but does not require the OMS to pre-compute it. The assembler (story 06) decides whether to use this helper or trust a repository-supplied value: when the repository populates `DrawdownState.drawdown_by_source_pct`, the helper is unused; when the repository leaves it empty (production design choice), the assembler invokes this helper and writes the result.

Per `feedback_simplify_before_building.md`, no class is introduced. Three pure functions in one module.

Per `feedback_avoid_numeric_anchors.md`, no threshold is hardcoded. The "negative `unrealized_pnl_usd` contributes to drawdown" rule is a structural definition, not a numeric anchor.

Per `feedback_no_inventing_component_names.md`, function names match the field names they produce.

The position_modification_trail is not consulted here — drawdown attribution operates on current state, not historical activity. Historical-window drawdown source attribution (e.g., "which positions contributed to the worst drawdown last week") is a feedback-loop concern, not a snapshot concern.

The `total_portfolio_value_usd` argument to `compute_portfolio_pnl` is supplied by the caller rather than computed inside, so the assembler can compute it once and pass the same value into per-position weight computation (story 05a) and this rollup. Keeping it as an explicit argument also makes the function trivially testable without a `CashLedger` fixture.

`abs()` on `current_market_value_usd` in `compute_total_portfolio_value_usd` reflects the convention that short positions' market values are signed negative per story 05a. The "total portfolio value" in the design's portfolio-weight context is the magnitude of capital deployed plus cash, not signed net exposure.

## Acceptance criteria

- [ ] `compute_portfolio_pnl(open_positions, inputs, total_portfolio_value_usd)` returns a `PortfolioPnL` with the documented field values; rollup math validated on a known fixture.
- [ ] `compute_portfolio_pnl` raises `ValueError` with the position_id when any open position has `unrealized_pnl_usd is None`.
- [ ] `compute_portfolio_pnl` returns `0.0` for `total_unrealized_pnl_pct_of_portfolio` when `total_portfolio_value_usd == 0`.
- [ ] `compute_portfolio_pnl` raises `KeyError` naming the missing key when `inputs.rolling_realized_pnl` lacks `"1d"`, `"3d"`, `"5d"`, or `"20d"`.
- [ ] `compute_portfolio_pnl` raises `ValueError` for negative `total_portfolio_value_usd`.
- [ ] `compute_drawdown_by_source_pct` returns the documented per-position contribution split when there is a non-zero drawdown and at least one negative-P/L position.
- [ ] `compute_drawdown_by_source_pct` returns `{}` when `current_drawdown_pct == 0.0`.
- [ ] `compute_drawdown_by_source_pct` returns `{}` when no positions have `unrealized_pnl_usd < 0`.
- [ ] `compute_drawdown_by_source_pct` raises `ValueError` for negative `current_drawdown_pct`.
- [ ] `compute_drawdown_by_source_pct` raises `ValueError` (with the position_id) when a position has `unrealized_pnl_usd is None`.
- [ ] `compute_total_portfolio_value_usd` returns `cash + sum(abs(market_value)) over open ∪ pending`; raises `ValueError` (with position_id) when any position has `current_market_value_usd is None`.
- [ ] All three functions are deterministic: identical inputs produce identical outputs across repeated calls.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
