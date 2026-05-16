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
    sector_members: dict[str, Sequence[str]],
    as_of: datetime,
) -> list[OutputBlock]:
    """Compute the breadth-and-internals block.

    Session-accepting thin shim that delegates to the pure compute under
    pre-loaded inputs. The broad-market ETF (``SPY``) and the sector
    roster are pinned in :mod:`._loaders`; this shim threads the
    universe-scope tickers and per-sector membership through.
    """
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
