# 03a — Reg T per-leg margin formula

## Goal

Ship the pure function `compute_regt_margin(positions, underlying_prices)` that computes the portfolio-aggregate Reg T initial-margin requirement summed across positions, using the per-leg formulas pinned in `venue-configuration.md`. Long equity = 50% of market value; short equity = 150%; long options = 100% of premium; short options = the FINRA Reg T formula encoded as definitional named constants per parent decision (A). The orchestrator in story 05 calls this for both the `*_before` and `*_after` snapshots.

## Reading

* `docs/design/05-execution-layer/regt-margin-attribution.md` § Computation flow — `regt_margin_before` and `regt_margin_after` are portfolio-aggregate Reg T requirements summed across the position set, computed against the pre-fill and post-fill states.
* `docs/design/05-execution-layer/venue-configuration.md` § Regulatory and account constraints — the per-leg Reg T tiers (Long equity 50% / Short equity 150% / Options buying 100% / Short options Alpaca formula); these are not operator-tunable and live in code.
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord`, `Direction.LONG`/`SHORT`, `EquityPositionDetails`, `OptionsPositionDetails`, `StrategyPositionDetails`, `StrategyLeg`. Identify the quantity / multiplier / premium / strike / underlying-price-context fields per variant.
* `docs/design/05-execution-layer/position-model.md` § Exposure calculations: delta-adjusted — confirms options carry a 100× contract multiplier and `quantity` is contract count for options.
* `ALP-126` parent issue § Pre-resolved decision (A) — FINRA Reg T short-options formula and its parameters.

## Depends on

* `ALP-421` (01a — Package skeleton + IBKR-mirror v1 config) — package must exist; this module reads no config (per-leg percentages are named constants), but file placement requires the package.

## Scope

In scope: `src/alphamind/execution/regt_margin_attribution/regt_margin.py` and `tests/execution/regt_margin_attribution/test_regt_margin.py`.

### 1\. Per-leg margin named constants

In `regt_margin.py`, define module-level constants:

```python
_LONG_EQUITY_INITIAL_MARGIN_PCT = 0.50
_SHORT_EQUITY_INITIAL_MARGIN_PCT = 1.50
_LONG_OPTION_INITIAL_MARGIN_PCT = 1.00
_SHORT_OPTION_OTM_BASE_PCT = 0.20  # 20% of underlying minus OTM amount
_SHORT_OPTION_FLOOR_PCT = 0.10     # 10% of underlying floor
_OPTION_CONTRACT_MULTIPLIER = 100
```

Constants are private; the function exposes the summed result, not the percentages.

### 2\. `compute_regt_margin` function

```python
def compute_regt_margin(
    positions: tuple[PositionRecord, ...],
    underlying_prices: Mapping[str, float],
) -> float:
    """Sum Reg T initial-margin requirements across the position set.

    Pure function. Returns the portfolio-aggregate Reg T requirement in
    USD. ``positions`` is the open-position set (Status.OPEN); pending /
    closed positions contribute zero by exclusion. ``underlying_prices``
    is a Mapping[symbol, current price] used for equity market-value and
    short-options-formula inputs.

    Per-leg formulas (``venue-configuration.md § Margin tiers``):

    * Long equity:  ``0.50 × |quantity| × underlying_price``
    * Short equity: ``1.50 × |quantity| × underlying_price``
    * Long options: ``1.00 × |contracts| × premium × 100``
      (premium is the option's mark/cost-basis price per contract)
    * Short options (FINRA Reg T, parent decision (A)):
      ``max(
          underlying_price × 0.20 − OTM_amount,
          underlying_price × 0.10,
          option_premium,
      ) × |contracts| × 100``
      where ``OTM_amount = max(strike − underlying, 0)`` for calls and
      ``max(underlying − strike, 0)`` for puts.

    Strategy positions iterate over their legs; each leg contributes per
    the long/short option formulas (legs are option instruments). The
    per-leg sum gives the strategy's Reg T contribution.

    Raises ``KeyError`` if a position's underlying symbol is missing from
    ``underlying_prices`` — that is an upstream contract violation
    (Phase 1 driver is responsible for populating prices for every
    open-position underlying).
    """
```

The function dispatches on the position-details variant (`EquityPositionDetails` / `OptionsPositionDetails` / `StrategyPositionDetails`), reads quantity / option fields / underlying symbol per variant, and sums contributions. No special-casing for hedged books: Reg T charges per leg by definition (the whole point of this attribution module).

### 3\. Tests

`tests/execution/regt_margin_attribution/test_regt_margin.py`:

* `test_empty_positions_returns_zero` — `compute_regt_margin((), {})` returns `0.0`.
* `test_long_equity_margin_is_50_percent_of_market_value` — 100 shares of NVDA at $500 → `50.0% × 100 × 500 = 25_000.0`.
* `test_short_equity_margin_is_150_percent_of_market_value` — short 100 shares NVDA at $500 → `150.0% × 100 × 500 = 75_000.0`.
* `test_long_option_margin_is_100_percent_of_premium_times_multiplier` — long 5 NVDA calls at $12 premium → `100% × 5 × 12 × 100 = 6_000.0`.
* `test_short_option_call_atm_uses_20_percent_branch` — short 1 NVDA call, strike=500, underlying=500, premium=$10 → OTM=0; max(500×0.20−0, 500×0.10, 10) × 1 × 100 = max(100, 50, 10) × 100 = 10_000.0.
* `test_short_option_call_deep_otm_uses_10_percent_floor` — short 1 NVDA call, strike=600, underlying=500, premium=$1 → OTM=100; max(500×0.20−100, 500×0.10, 1) × 1 × 100 = max(0, 50, 1) × 100 = 5_000.0.
* `test_short_option_call_extreme_otm_uses_premium_floor` — short 1 NVDA call, strike=1000, underlying=500, premium=$0.50 → OTM=500; max(500×0.20−500, 500×0.10, 0.5) × 1 × 100 = max(-400, 50, 0.5) × 100 = 5_000.0 (still floored by 10% rule). Add a contrived test where 10%-floor < premium to exercise that branch: e.g., underlying=10, strike=100, premium=$5 → max(10×0.20−90, 10×0.10, 5) = max(-88, 1, 5) = 5; assert that branch.
* `test_short_option_put_uses_strike_minus_underlying_for_otm` — short 1 NVDA put, strike=400, underlying=500, premium=$1 → OTM=100 (strike − underlying = -100, but put OTM = underlying − strike = 100); max(500×0.20−100, 500×0.10, 1) × 1 × 100 = max(0, 50, 1) × 100 = 5_000.0.
* `test_strategy_position_sums_per_leg_margins` — NVDA short iron condor (4 legs: 2 short, 2 long). Assert the result equals the sum of per-leg contributions.
* `test_mixed_portfolio_sums_correctly` — long AAPL stock + short NVDA call + long QQQ put. Assert the result equals the sum of the three individual contributions.
* `test_missing_underlying_price_raises_key_error` — position with `symbol="ZZZZ"` not in `underlying_prices` raises `KeyError`.
* `test_pending_position_excluded_from_margin` — a `PositionStatus.PENDING` position is excluded (passes filter by status, contributes zero).

### Out of scope

* PM-equivalent stress — story 03b.
* Class-group composition — story 02 (Reg T iterates positions directly).
* Conservative buffers, delta adjustment, or any guardrail-evaluation primitives — Reg T per-leg is unmodified by delta/IV/regime.
* Maintenance margin — design specifies initial margin only.

## Acceptance criteria

- [ ] `compute_regt_margin` is defined in `src/alphamind/execution/regt_margin_attribution/regt_margin.py` with the keyword-only signature shape named in Scope §2.
- [ ] Per-leg percentages live as named module-level constants per Scope §1; no magic numbers in the function body.
- [ ] Long equity contributes `0.50 × |quantity| × underlying_price` to the sum.
- [ ] Short equity contributes `1.50 × |quantity| × underlying_price`.
- [ ] Long options contribute `100% × |contracts| × premium × 100`.
- [ ] Short options contribute the FINRA Reg T formula `max(underlying × 0.20 − OTM_amount, underlying × 0.10, premium) × |contracts| × 100` with the correct OTM definition for calls vs. puts.
- [ ] Strategy positions iterate over their legs and sum per-leg contributions.
- [ ] Pending / closed positions are excluded from the sum.
- [ ] A missing underlying-price entry raises `KeyError` naming the missing symbol.
- [ ] `compute_regt_margin` is a pure function (no I/O, no clock reads, no global mutation).
- [ ] `compute_regt_margin` is exposed via `src/alphamind/execution/regt_margin_attribution/__init__.py`'s `__all__`.
- [ ] `tests/execution/regt_margin_attribution/test_regt_margin.py` covers the twelve tests named in Scope §3 and passes under `uv run pytest tests/execution/regt_margin_attribution/ -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` are clean.

## Verification

Run `uv run pytest tests/execution/regt_margin_attribution/test_regt_margin.py -n auto`. Spot-check `regt_margin.py` to confirm the FINRA short-options formula reads from the named constants (no inline 0.20 / 0.10 / 100 literals) and the OTM-amount branch correctly distinguishes calls from puts.