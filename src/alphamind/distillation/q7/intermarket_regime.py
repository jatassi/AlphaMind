"""Thin orchestration shim for the Q7 intermarket regime blocks (ALP-486).

The pure compute lives in :mod:`.intermarket_regime_compute`; the
session-bound IO shell helpers live in :mod:`._loaders`. This module
preserves the legacy session-accepting public API and re-exports the
shared series-identifier constants.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._loaders import _load_intermarket_blocks
from alphamind.distillation.q7.intermarket_regime_compute import (
    GLD_TICKER,
    OIL_SERIES,
    OIL_SOURCE,
    REAL_YIELD_SERIES,
    REAL_YIELD_SOURCE,
    SPY_TICKER,
    TLT_TICKER,
    VIX_SERIES,
    VIX_SOURCE,
    XLE_TICKER,
    IntermarketRegimeInputs,
    compute_intermarket_regime_pure,
)


def compute_intermarket_regime(
    session: Session,
    *,
    as_of: datetime,
    window_days: int,
    short_window_days: int,
) -> list[OutputBlock]:
    """Compute the four intermarket regime blocks.

    Session-accepting thin shim that delegates to the pure compute under
    pre-loaded inputs.
    """
    blocks = _load_intermarket_blocks(
        session,
        as_of=as_of,
        window_days=window_days,
        short_window_days=short_window_days,
    )
    return list(blocks)


__all__ = [
    "GLD_TICKER",
    "OIL_SERIES",
    "OIL_SOURCE",
    "REAL_YIELD_SERIES",
    "REAL_YIELD_SOURCE",
    "SPY_TICKER",
    "TLT_TICKER",
    "VIX_SERIES",
    "VIX_SOURCE",
    "XLE_TICKER",
    "IntermarketRegimeInputs",
    "compute_intermarket_regime",
    "compute_intermarket_regime_pure",
]
