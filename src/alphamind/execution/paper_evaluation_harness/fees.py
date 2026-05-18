"""Regulatory fee estimator for paper-evaluation harness (ALP-524).

Pure function: no DB, no HTTP, no config loading. Takes a completed fill's
instrument type, side, quantity, price, and a FeeSchedule, and returns the
estimated regulatory fees as a non-negative Money value.

Fee taxonomy by instrument class and side (from broker-adapter.md § Fee
reporting and paper-evaluation-harness.md § Regulatory fees):

* Equities:
  - CAT (Consolidated Audit Trail) — all fills, per executed share
  - TAF (FINRA Trading Activity Fee) — sells only, per share
  - SEC (Section 31) — sells only, per dollar of notional

* Options:
  - CAT — all fills; applied per *underlying share* (contracts * 100)
  - SEC — sells only, per dollar of notional (notional = price * contracts * 100)
  - ORF (Options Regulatory Fee) — all fills, per contract
  - OCC (Options Clearing Corporation) — all fills, per contract

* Strategy (STRATEGY InstrumentType):
  - Unreachable at this layer: strategy fills enter the wedge decomposed
    into per-leg EQUITY/OPTIONS fills. Raises NotImplementedError to fail
    loudly if the wedge contract changes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from alphamind._kernel.money import Money, Price, money
from alphamind.config.models.execution import FeeSchedule
from alphamind.portfolio_state.records.positions import InstrumentType

_SHARES_PER_OPTIONS_CONTRACT = Decimal(100)


def compute_regulatory_fees(
    *,
    instrument_type: InstrumentType,
    side: Literal["buy", "sell"],
    fill_quantity: float,
    fill_price: Price,
    schedule: FeeSchedule,
) -> Money:
    """Return the estimated regulatory fees for a single paper fill.

    Parameters
    ----------
    instrument_type:
        EQUITY or OPTIONS. STRATEGY raises NotImplementedError — strategy
        fills must be decomposed into per-leg fills before reaching this
        function.
    side:
        ``"buy"`` or ``"sell"``.
    fill_quantity:
        Number of shares (EQUITY) or contracts (OPTIONS) filled.
    fill_price:
        Fill price per share (EQUITY) or per contract (OPTIONS).
    schedule:
        Regulatory fee rates sourced from the execution config.

    Returns
    -------
    Money
        Total estimated regulatory fees. Always non-negative.
    """
    qty = Decimal(str(fill_quantity))

    if instrument_type is InstrumentType.EQUITY:
        return _equity_fees(side=side, qty=qty, fill_price=fill_price, schedule=schedule)
    if instrument_type is InstrumentType.OPTIONS:
        return _options_fees(side=side, qty=qty, fill_price=fill_price, schedule=schedule)

    msg = (
        f"compute_regulatory_fees called with instrument_type={instrument_type!r}; "
        "STRATEGY fills must be decomposed into per-leg EQUITY/OPTIONS fills "
        "before reaching the fee estimator."
    )
    raise NotImplementedError(msg)


def _equity_fees(
    *,
    side: Literal["buy", "sell"],
    qty: Decimal,
    fill_price: Price,
    schedule: FeeSchedule,
) -> Money:
    cat_rate = Decimal(str(schedule.cat_per_executed_share))
    total = cat_rate * qty

    if side == "sell":
        taf_rate = Decimal(str(schedule.taf_per_share_sells))
        sec_rate = Decimal(str(schedule.sec_pct_of_notional_sells))
        notional = fill_price * qty
        total += taf_rate * qty + sec_rate * notional

    return money(total)


def _options_fees(
    *,
    side: Literal["buy", "sell"],
    qty: Decimal,
    fill_price: Price,
    schedule: FeeSchedule,
) -> Money:
    # CAT applies per underlying share for options (contracts * 100)
    cat_rate = Decimal(str(schedule.cat_per_executed_share))
    underlying_shares = qty * _SHARES_PER_OPTIONS_CONTRACT
    total = cat_rate * underlying_shares

    if side == "sell":
        sec_rate = Decimal(str(schedule.sec_pct_of_notional_sells))
        # Options notional: fill_price (per-contract premium) * contracts * 100
        notional = fill_price * underlying_shares
        total += sec_rate * notional

    orf_rate = Decimal(str(schedule.orf_per_options_contract))
    occ_rate = Decimal(str(schedule.occ_per_options_contract))
    total += (orf_rate + occ_rate) * qty

    return money(total)
