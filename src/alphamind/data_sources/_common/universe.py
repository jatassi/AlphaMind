"""Asset-universe ticker lookup helpers.

Reads the active ticker scope from the ``asset_universe`` table.
"""

from __future__ import annotations

from typing import Any

from alphamind.data_sources._common.run_tracking import default_session_factory

__all__ = ["active_universe_tickers"]


def active_universe_tickers(
    *,
    include_benchmarks: bool = True,
    session_factory: Any = None,
) -> list[str]:
    """Return active tickers from ``asset_universe``.

    Includes ``asset_role='universe'`` rows always; benchmark roles
    (``benchmark`` / ``broad_market`` / ``intermarket`` / ``sector_etf`` /
    ``breadth``) are included by default.
    """
    from alphamind.persistence.models import AssetUniverse

    if session_factory is None:
        session_factory = default_session_factory()

    with session_factory() as sess:
        q = sess.query(AssetUniverse.ticker).filter(AssetUniverse.is_active == 1)
        if not include_benchmarks:
            q = q.filter(AssetUniverse.asset_role == "universe")
        return [r.ticker for r in q.all()]
