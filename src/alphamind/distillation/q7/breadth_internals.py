"""Thin orchestration shim for the Q7 breadth-internals block (ALP-486).

The pure compute lives in :mod:`.breadth_internals_compute`; the
session-bound IO shell helpers live in :mod:`._loaders`. This module
preserves the legacy session-accepting public API.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._loaders import _load_breadth_blocks
from alphamind.distillation.q7.breadth_internals_compute import (
    EMA_WINDOWS_DAYS,
    compute_breadth_internals_pure,
)


def compute_breadth_internals(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    sectors: Sequence[str],
    sector_members: dict[str, Sequence[str]],
    broad_market_etf: str,
    as_of: datetime,
) -> list[OutputBlock]:
    """Compute the breadth-and-internals block.

    Session-accepting thin shim that delegates to the pure compute under
    pre-loaded inputs. ``broad_market_etf`` is fixed to ``SPY`` by the
    upstream pipeline; the parameter is preserved for back-compat.
    ``sectors`` is informational; ``sector_members`` drives the per-sector
    advance/decline counts the payload exposes.
    """
    del sectors, broad_market_etf  # informational; defaults pinned in the pure compute path
    blocks = _load_breadth_blocks(
        session,
        ticker_scope=universe_tickers,
        sector_roster=sector_members,
        as_of=as_of,
    )
    return list(blocks)


__all__ = [
    "EMA_WINDOWS_DAYS",
    "compute_breadth_internals",
    "compute_breadth_internals_pure",
]
