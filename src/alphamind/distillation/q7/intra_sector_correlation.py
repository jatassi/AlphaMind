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
from alphamind.distillation.q7._loaders import _load_intra_sector_blocks
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

    Session-accepting thin shim. Delegates to :func:`_load_intra_sector_blocks`
    with a single-sector roster; the loader reads per-ticker closes, runs
    the pure compute, persists the divergence events, and flushes the
    session.
    """
    blocks = _load_intra_sector_blocks(
        session,
        sector_roster={sector: tuple(sector_tickers)},
        as_of=as_of,
        short_window_days=short_window_days,
        long_window_days=long_window_days,
        divergence_sigma=divergence_sigma,
    )
    return list(blocks)


__all__ = [
    "compute_intra_sector_correlation",
    "compute_intra_sector_correlation_pure",
]
