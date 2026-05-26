# 03b — Reg T margin: defined-risk strategy capped-loss

# 03b — Reg T margin: defined-risk strategy capped-loss

## Goal

Fix `_strategy_position_margin` in `execution/regt_margin_attribution/regt_margin.py` so a defined-risk strategy (a strategy with a finite max loss — vertical spreads, iron condors) requires margin equal to the strategy's capped loss, not the sum of per-leg naked margin. Today it sums `_option_leg_margin` over the legs with no spread offset, so a defined-risk credit spread is vastly over-margined — exactly what the design doc calls the wrong treatment ("strategies with capped loss require margin equal to the strategy's max loss, not individual legs").

## Reading

* `src/alphamind/execution/regt_margin_attribution/regt_margin.py` — `_strategy_position_margin` (sums per-leg `_option_leg_margin` with no offset), `_option_leg_margin`, `compute_regt_margin` (sums only `PositionStatus.OPEN` positions), the per-leg margin constants.
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyPositionDetails.max_loss_usd` (populated by story 02; negative for a loss, `-inf` for unbounded downside).
* `docs/design/05-execution-layer/regt-margin-attribution.md` and `docs/design/05-execution-layer/position-model.md` § Margin model — "Defined-risk strategies ... require margin equal to the strategy's max loss".
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) — the Current-state table row for `regt_margin.py:76`.

## Depends on

* 02 ([ALP-598](https://linear.app/alphamind-jatassi/issue/ALP-598/02-phase-1-strategy-entry-fill-payoff-recompute)) — `_strategy_position_margin` reads `max_loss_usd`, which story 02 populates on the Phase 1 entry fill.

## Scope

In scope: `src/alphamind/execution/regt_margin_attribution/regt_margin.py`. Tests at `tests/execution/regt_margin_attribution/`.

### 1\. Defined-risk strategy margin = capped loss

`_strategy_position_margin` returns:

* For a **defined-risk** strategy — `max_loss_usd` finite — the margin is `abs(max_loss_usd)`: the most the position can lose, which is exactly the collateral a broker requires for a defined-risk structure.
* For an **undefined-risk** strategy — `max_loss_usd == float('-inf')` (a naked short component, e.g. an uncovered short strangle side) — fall back to the existing per-leg `_option_leg_margin` sum, the conservative naked-margin treatment.

`max_loss_usd` is the signed worst-case P/L (a loss → negative); margin is a positive USD requirement, hence `abs(...)`. A defined-risk credit spread's `abs(max_loss_usd)` equals (strike width × multiplier × contracts) minus the credit received — far below the summed naked margin.

### 2\. Skeleton / malformed-record guard

If a strategy's `max_loss_usd` is `0.0`, fall back to the per-leg sum (not zero margin). This is defensive — `compute_regt_margin` only sums OPEN positions and an OPEN strategy has story-02-recomputed metrics, so a `0.0` here means a malformed record rather than a normal state.

### Out of scope

`_equity_margin`, `_option_leg_margin`, and single-option margin — unchanged. The per-leg margin constants — unchanged. `max_loss_usd` population — story 02.

## Acceptance criteria

- [ ] For a defined-risk strategy (finite `max_loss_usd`), `_strategy_position_margin` returns `abs(max_loss_usd)`.
- [ ] A defined-risk credit vertical spread's Reg T margin equals the spread's capped loss and is far below the sum of its per-leg naked margin.
- [ ] For an undefined-risk strategy (`max_loss_usd == float('-inf')`), `_strategy_position_margin` falls back to the per-leg `_option_leg_margin` sum.
- [ ] A strategy with `max_loss_usd == 0.0` falls back to the per-leg sum, not zero margin.
- [ ] `compute_regt_margin` over a position set containing a defined-risk strategy returns the capped-loss contribution for it; equity and single-option contributions are unchanged.
- [ ] Tests at `tests/execution/regt_margin_attribution/` cover a defined-risk credit spread and an undefined-risk short strangle; pass under `uv run pytest tests/execution/regt_margin_attribution/ --testmon -n auto`.

## Verification

`uv run pytest tests/execution/regt_margin_attribution/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. Spot-check: a 5-point-wide bull put spread sold for credit margins at (5 × 100 × contracts − credit), not the summed short-put + long-put naked margin.
