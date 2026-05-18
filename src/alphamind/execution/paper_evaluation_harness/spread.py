"""Spread estimator for paper-evaluation harness (ALP-525).

Pure function: no DB, no HTTP, no module-level config imports. Estimates the
bid-ask spread a paper fill would have crossed at live execution time, using
a price/ADV/volatility fallback when an observed quote is unavailable.

Model form. The estimator combines an Amihud-illiquidity-style 1/sqrt(ADV)
shape with a volatility-scaled half-spread proxy, floored by the NMS Rule
612 penny-tick minimum for actively-traded equities:

    adv_dollars        = max(adv_shares, _ADV_SHARES_EPSILON) * fill_price
    adv_dollars_scaled = adv_dollars / _ADV_DOLLAR_SCALE        (per-$M)
    spread_raw         = _K * fill_price * realized_volatility
                       / sqrt(adv_dollars_scaled)
    spread_floored     = max(_MIN_SPREAD_FLOOR_DOLLARS, spread_raw)
    spread             = spread_floored * (1 + buffer_pct / 100)

Rationale and citations.

* The ``1 / sqrt(adv_dollars)`` shape is the Amihud (2002) illiquidity
  proxy in spread-cost form — illiquidity is roughly inversely
  proportional to the square root of dollar volume, capturing the
  empirical regularity that doubling ADV does not halve the spread.
* Scaling ``fill_price * realized_volatility`` into the numerator
  encodes the Corwin-Schultz (2012) high-low spread estimator's
  observation that effective spreads scale with intraday price-range
  variance — higher-vol names trade with wider quotes at the same
  liquidity tier.
* The ``per-$M`` ADV scaling (``adv_dollars / 1_000_000``) is a unit
  convenience: it puts the sqrt-denominator on an O(1-10000) scale for
  liquid-to-illiquid US equities, so ``_K`` lives near the empirically
  fit 0.5.
* The penny-tick floor of $0.01 reflects SEC Rule 612 (Regulation NMS)
  minimum quote increment for NMS securities at or above $1.00. Active
  large-caps quote tighter intraday, but real fills at the touch
  experience cross-spread cost no smaller than one tick on average.

Constants are model parameters (not operator knobs) per the parent
decision E and the harness's calibrated-not-pessimistic principle —
they are calibrated by the impact-coefficient feedback loop downstream
(``paper-evaluation-harness.md`` § Calibration path), not tuned in YAML.
"""

from __future__ import annotations

from decimal import Decimal

from alphamind._kernel.money import Money, Price

__all__ = ["estimate_spread"]

# Empirical scaling constant for the Amihud-style 1/sqrt(ADV-$M) shape.
# Sized so that an AAPL-shape input (200 px, 50M ADV, 20% vol) lands near a
# realistic 10-20 bps half-spread before the +10% intraday-widening buffer.
_K = Decimal("0.5")

# Penny-tick floor — SEC Reg NMS Rule 612 minimum quote increment for
# NMS securities at or above $1.00.
_MIN_SPREAD_FLOOR_DOLLARS = Decimal("0.01")

# Dollar-volume scaling: convert ADV-dollars to dollars-per-million so the
# sqrt denominator is on an O(1-10000) scale for typical US equities.
_ADV_DOLLAR_SCALE = Decimal(1000000)

# Defensive minimum for the input clamps. ADV/price of 0 means data was
# unavailable upstream — the wedge layer should have caught that. The
# clamp prevents a div-by-zero crash in the harness's pure path.
_ADV_SHARES_EPSILON = Decimal(1)
_PRICE_EPSILON = Decimal("0.01")


def estimate_spread(
    *,
    fill_price: Price,
    adv_shares: float,
    realized_volatility: float,
    buffer_pct: float,
) -> Money:
    """Estimate the bid-ask spread a paper fill would have crossed at live execution.

    Returns a Money value representing the per-share (or per-contract for
    options — the caller supplies the right ``adv_shares`` semantics) dollar
    spread, multiplied by the configured intraday-widening buffer.

    The function is pure: no DB, no HTTP, no module-level config imports.
    All inputs flow through parameters.

    Parameters
    ----------
    fill_price:
        Paper fill price per share (equities) or per contract (options).
        Clamped to a small positive epsilon before the model to avoid
        a zero-price div in the (defensive) unhappy path.
    adv_shares:
        Average daily volume in shares (equities) or contracts (options),
        sourced upstream from the data-pipeline ADV projection. Clamped
        to ``_ADV_SHARES_EPSILON`` (= 1) before the sqrt — the wedge
        should have caught a zero/None ADV earlier, but this function
        does not crash.
    realized_volatility:
        Annualized realized volatility (decimal form — e.g., 0.20 for
        20%). ``0`` saturates the spread at the floor via the ``max``
        clamp.
    buffer_pct:
        Intraday-widening buffer in percent (10 == +10%). Calibrator,
        not safety factor; sourced from ``PaperHarness.spread_buffer_pct``
        in execution.yaml.

    Returns
    -------
    Money
        Non-negative spread in dollars. Always >= floor * buffer.
    """
    # Clamp inputs defensively. The wedge layer should have surfaced
    # missing-data conditions earlier; these guards prevent a crash if
    # the contract is breached.
    price_decimal = Decimal(fill_price)
    if price_decimal < _PRICE_EPSILON:
        price_decimal = _PRICE_EPSILON

    adv_decimal = Decimal(str(adv_shares))
    if adv_decimal < _ADV_SHARES_EPSILON:
        adv_decimal = _ADV_SHARES_EPSILON

    vol_decimal = Decimal(str(realized_volatility))
    buffer_decimal = Decimal(str(buffer_pct))

    # Amihud-illiquidity-style spread proxy: spread widens as 1/sqrt(ADV-$),
    # scales with price * realized volatility (Corwin-Schultz shape).
    adv_dollars_scaled = (adv_decimal * price_decimal) / _ADV_DOLLAR_SCALE
    sqrt_adv = adv_dollars_scaled.sqrt()
    raw_spread = (_K * price_decimal * vol_decimal) / sqrt_adv

    # Penny-tick floor (NMS Rule 612). Saturates on zero-vol / zero-price.
    floored = max(_MIN_SPREAD_FLOOR_DOLLARS, raw_spread)

    # Apply the intraday-widening buffer (calibrator, not safety factor).
    buffered = floored * (Decimal(1) + buffer_decimal / Decimal(100))

    return Money(buffered)
