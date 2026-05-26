# 03a — Strategy unrealized P/L percentage (non-inverting denominator)

# 03a — Strategy unrealized P/L percentage (non-inverting denominator)

## Goal

Fix the inverted strategy P/L percentage. The assembler computes a strategy's `unrealized_pnl_pct` by dividing P/L USD by `cost_basis = net_premium_usd`, which is negative for a net credit — so a profitable credit strategy displays a negative percentage (shows as a loss), and the loss-zone classifier then fires WARNING/CRITICAL on a winner. The P/L USD is already correct (signed market value minus net premium); only the percentage inverts. Fix: a strategy's P/L percentage uses the magnitude of `max_loss_usd` (capital at risk) as the denominator — non-inverting, uniform for debit and credit.

## Reading

* `src/alphamind/portfolio_state/assembler.py` — `_price_fields_strategy` (sets `cost_basis = details.net_premium_usd`), `_enrich_position_first_pass` (calls `compute_unrealized_pnl_usd` then `compute_unrealized_pnl_pct(unrealized_pnl_usd, pf.cost_basis)`), the `_PriceFields` carrier.
* `src/alphamind/portfolio_state/computations/positions.py` — `compute_unrealized_pnl_usd` (for a strategy's LONG direction this yields `signed_mv − net_premium`, already correct) and `compute_unrealized_pnl_pct` (divides by `cost_basis_usd`; returns `0.0` when `cost_basis_usd == 0`).
* `src/alphamind/risk_guardrails/state_delivery/primitives.py` — `_classify_loss_zone(pnl_pct, max_loss_pct)` — the downstream consumer of the percentage.
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyPositionDetails.max_loss_usd` (populated by story 02; a loss — negative, or `-inf` for unbounded downside).
* `docs/design/05-execution-layer/position-model.md` § Strategy position (per story 01a) — capital at risk = magnitude of max loss.
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Pre-resolved decision (B).

## Depends on

* 02 ([ALP-598](https://linear.app/alphamind-jatassi/issue/ALP-598/02-phase-1-strategy-entry-fill-payoff-recompute)) — `max_loss_usd` is the percentage denominator and is populated by story 02; before 02 a live strategy carries the OPEN skeleton's `0.0`.

## Scope

In scope: `src/alphamind/portfolio_state/assembler.py` and `src/alphamind/portfolio_state/computations/positions.py`. Tests under `tests/portfolio_state/`.

### 1\. Strategy P/L percentage denominator

For a STRATEGY position, the unrealized P/L percentage is `unrealized_pnl_usd / abs(max_loss_usd) * 100`. The USD P/L is unchanged — `compute_unrealized_pnl_usd` with the strategy's LONG direction already yields the correct `signed_mv − net_premium`. Only the percentage denominator changes: from `net_premium_usd` (signed, inverts on a credit) to `abs(max_loss_usd)` (capital at risk, always positive).

Implement either as a strategy-specific `compute_strategy_unrealized_pnl_pct(unrealized_pnl_usd, max_loss_usd)` in `computations/positions.py` with the assembler branching on `instrument_type == STRATEGY`, or by threading a strategy-specific denominator through `_PriceFields`. Equity and single-leg option P/L percentage keep dividing by their own `cost_basis` — unchanged. When `max_loss_usd` is `0.0` (skeleton / not yet recomputed) or unbounded (`-inf`), the percentage is `0.0` — no division blow-up, consistent with the existing zero-cost-basis guard and parent decision B.

### 2\. Loss-zone classifier — verify

`_classify_loss_zone` consumes the P/L percentage. Once the strategy percentage is non-inverting, a winning credit strategy has a positive `pnl_pct`, so `_classify_loss_zone`'s `loss_progress = -pnl_pct / max_loss_pct` is non-positive and the zone stays NORMAL. Confirm by test; no code change is expected in `primitives.py` — it is a correct consumer of a now-correct percentage.

### Out of scope

Equity / single-leg option P/L percentage. `max_loss_usd` population (story 02). Strategy P/L USD (already correct).

## Acceptance criteria

- [ ] A net-credit strategy with positive unrealized P/L USD has a positive `unrealized_pnl_pct`.
- [ ] A net-credit strategy with negative unrealized P/L USD has a negative `unrealized_pnl_pct`.
- [ ] The strategy P/L percentage denominator is `abs(max_loss_usd)`, not `net_premium_usd`.
- [ ] A strategy with `max_loss_usd == 0.0` or `max_loss_usd == float('-inf')` yields `unrealized_pnl_pct == 0.0` with no division error.
- [ ] Equity and single-leg option `unrealized_pnl_pct` are unchanged (regression-covered).
- [ ] `_classify_loss_zone` returns NORMAL — not WARNING or CRITICAL — for a winning credit strategy (positive P/L), covered by a test.
- [ ] Tests under `tests/portfolio_state/` and a `_classify_loss_zone` test under `tests/risk_guardrails/` pass under `uv run pytest tests/portfolio_state/ tests/risk_guardrails/ --testmon -n auto`.

## Verification

`uv run pytest tests/portfolio_state/ tests/risk_guardrails/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. Spot-check: an iron condor sold for credit, currently profitable, shows a positive `unrealized_pnl_pct` and a NORMAL loss zone.
