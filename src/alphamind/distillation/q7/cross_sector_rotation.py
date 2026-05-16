"""Thin orchestration shim for the Q7 cross-sector rotation block (ALP-486).

The pure compute lives in :mod:`.cross_sector_rotation_compute`; the
session-bound IO shell helpers live in :mod:`._loaders`. This module
preserves the legacy session-accepting public API and re-exports the
shared narrative / velocity constants.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._loaders import _load_cross_sector_blocks
from alphamind.distillation.q7.cross_sector_rotation_compute import (
    ROTATION_NARRATIVE_GROWTH_DRIVEN,
    ROTATION_NARRATIVE_RATE_DRIVEN,
    ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN,
    VELOCITY_SHARP,
    VELOCITY_SLOW,
    compute_cross_sector_rotation_pure,
)


def compute_cross_sector_rotation(
    session: Session,
    *,
    sector_etfs: Sequence[str],
    risk_proxies: Sequence[str],
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
) -> list[OutputBlock]:
    """Compute the cross-sector rotation block.

    Session-accepting thin shim. ``sector_etfs`` and ``risk_proxies`` are
    pinned in :mod:`._loaders` per story 08d's named ETF roster, so the
    arguments here are accepted for back-compat but not consulted; the
    underlying compute path uses the canonical roster.
    """
    del sector_etfs, risk_proxies
    blocks = _load_cross_sector_blocks(
        session,
        as_of=as_of,
        short_window_days=short_window_days,
        long_window_days=long_window_days,
    )
    return list(blocks)


__all__ = [
    "ROTATION_NARRATIVE_GROWTH_DRIVEN",
    "ROTATION_NARRATIVE_RATE_DRIVEN",
    "ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN",
    "VELOCITY_SHARP",
    "VELOCITY_SLOW",
    "compute_cross_sector_rotation",
    "compute_cross_sector_rotation_pure",
]
