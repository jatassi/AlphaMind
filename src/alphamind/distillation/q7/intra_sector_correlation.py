"""Thin orchestration shim for the Q7 intra-sector correlation block (ALP-486).

The pure compute lives in :mod:`.intra_sector_correlation_compute`; the
session-bound IO shell helpers live in :mod:`._loaders` and own the
``correlation_divergence`` event writes triggered by the pair-divergence
flags. This module preserves the legacy session-accepting public API.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._helpers import (
    _log_returns_from_closes,
    _window_bounds,
)
from alphamind.distillation.q7._loaders import (
    _persist_correlation_divergence_events,
    _select_close_series,
)
from alphamind.distillation.q7.intra_sector_correlation_compute import (
    compute_intra_sector_correlation_pure,
)


def compute_intra_sector_correlation(
    session: Session,
    *,
    sector: str,
    sector_tickers: Sequence[str],
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
    divergence_sigma: float,
) -> list[OutputBlock]:
    """Compute the per-sector intra-sector correlation block.

    Session-accepting thin shim that:

    1. Reads per-ticker daily-bar closes over the long window.
    2. Calls the pure compute to produce the block (matrices + divergence
       flags).
    3. Persists ``correlation_divergence`` event rows for each flag.
    4. Flushes the session so the row writes settle before any Phase 2
       parallel-compute consumer hits the session.
    """
    long_start, range_end = _window_bounds(as_of=as_of, window_days=long_window_days)
    long_returns: dict[str, tuple[float, ...]] = {}
    for ticker in sector_tickers:
        closes = _select_close_series(
            session, ticker=ticker, range_start=long_start, range_end=range_end
        )
        long_returns[ticker] = tuple(_log_returns_from_closes(closes))
    block = compute_intra_sector_correlation_pure(
        sector=sector,
        sector_tickers=sector_tickers,
        long_returns_by_ticker=long_returns,
        short_window_days=short_window_days,
        long_window_days=long_window_days,
        divergence_sigma=divergence_sigma,
        as_of=as_of,
    )
    _persist_correlation_divergence_events(session, block=block, as_of=as_of)
    session.flush()
    return [block]


__all__ = [
    "compute_intra_sector_correlation",
    "compute_intra_sector_correlation_pure",
]
