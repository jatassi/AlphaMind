"""Market-impact estimator for paper-evaluation harness (ALP-526).

Pure function: no DB, no HTTP, no config loading. Implements the square-root
market-impact model:

    impact = coefficient * estimated_spread * sqrt(fill_shares / adv_shares)

The coefficient is order-type dependent (market 0.5, limit 0.25, stop 0.75 by
default) and must be supplied by the caller (story 03), which looks it up from
``PaperHarness.impact_coefficients[order_type]``.

Edge-case contracts
-------------------
- ``fill_shares <= 0``  → returns ``money("0")`` (no shares, no impact).
- ``adv_shares <= 0``   → clamped to 1.0 before the division to avoid
  divide-by-zero / domain error in sqrt. This produces a conservative
  (large) impact estimate for the degenerate case.
- ``coefficient <= 0``  → raises ``ValueError``; a non-positive coefficient
  means a programmer error in the caller (``PaperHarness.coefficients_strictly_positive``
  validates config-loaded coefficients at startup).
"""

from __future__ import annotations

import math
from decimal import Decimal

from alphamind._kernel.money import Money, money
from alphamind.config.models.execution import OrderType

__all__ = ["estimate_impact"]

_DECIMAL_ZERO = Decimal(0)


def estimate_impact(
    *,
    fill_shares: float,
    adv_shares: float,
    estimated_spread: Money,
    order_type: OrderType,
    coefficient: float,
) -> Money:
    """Return the estimated market-impact cost for a single paper fill.

    Parameters
    ----------
    fill_shares:
        Number of shares in the fill (equities) or notional-equivalent shares
        (options — caller converts contracts * 100 before passing here).
    adv_shares:
        Average daily volume in shares for the instrument. Clamped to 1.0 if
        <= 0 to avoid domain errors; callers should supply real ADV data.
    estimated_spread:
        Estimated bid-ask spread in dollars, as a non-negative ``Money``
        value. Impact is proportional to the spread, so a zero spread yields
        zero impact.
    order_type:
        The order type; included so call sites are self-documenting and to
        allow future coefficient look-up logic if needed.
    coefficient:
        Scalar from ``PaperHarness.impact_coefficients[order_type]``. Must be
        strictly positive — a zero or negative value indicates a programmer
        error and raises ``ValueError``.

    Returns
    -------
    Money
        Estimated market-impact cost. Always non-negative.

    Raises
    ------
    ValueError
        If ``coefficient <= 0``.
    """
    if coefficient <= 0:
        msg = (
            f"estimate_impact: coefficient must be strictly positive; got {coefficient!r}. "
            f"Check that PaperHarness.impact_coefficients[{order_type!r}] is loaded correctly."
        )
        raise ValueError(msg)

    if fill_shares <= 0:
        # No shares filled → no impact.
        return money("0")

    if estimated_spread <= _DECIMAL_ZERO:
        # Impact is proportional to spread; zero spread → zero impact.
        return money("0")

    # Clamp adv_shares to 1.0 to avoid divide-by-zero / sqrt(negative).
    safe_adv = max(adv_shares, 1.0)

    scalar = coefficient * math.sqrt(fill_shares / safe_adv)
    # Convert via str to avoid binary-float drift in Decimal arithmetic.
    scalar_decimal = Decimal(str(scalar))

    return Money(estimated_spread * scalar_decimal)
