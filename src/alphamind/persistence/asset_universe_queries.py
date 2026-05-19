"""Shared ``asset_universe`` query primitives.

Session-shape-agnostic: each caller executes the returned ``Select`` against
its own ``Session`` / ``AsyncSession`` and applies :func:`coerce_adv_shares`
to the raw column value.
"""

from __future__ import annotations

from sqlalchemy import Select, select

from alphamind.persistence.models import AssetUniverse

__all__ = ["adv_shares_select", "coerce_adv_shares"]


def adv_shares_select(ticker: str) -> Select[tuple[int | None]]:
    """``SELECT avg_daily_volume_shares FROM asset_universe WHERE ticker = :ticker``.

    A one-row, one-column projection. ``one_or_none()`` returns ``None``
    when the ticker is absent from ``asset_universe`` and ``Row((None,))``
    when the row exists but the ADV column is NULL — both cases collapse to
    ``None`` after :func:`coerce_adv_shares`.
    """
    return select(AssetUniverse.avg_daily_volume_shares).where(AssetUniverse.ticker == ticker)


def coerce_adv_shares(adv: int | None) -> float | None:
    """Coerce the ``Mapped[int | None]`` ADV column to ``float | None``."""
    return float(adv) if adv is not None else None
