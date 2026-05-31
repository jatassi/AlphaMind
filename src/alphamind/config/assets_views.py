"""Per-invocation views over ``resolved.assets.sectors``.

The composition layer needs four parallel projections of the resolved
assets-config sector map:

* the set of active sector keys (decision-pipeline ``active_sectors``),
* a ticker → sector resolver (used by the synthesizer and the library
  snapshot translator),
* the alphabetized union of every sector's tickers (analysis-pipeline
  ``ticker_scope``),
* the per-sector ticker buckets (analysis-pipeline ``sectors_config``).

These four helpers lived inline in ``scheduler/orchestrator.py`` until
ALP-472; the lift homes them in the ``config`` layer they project from so
they can be shared with the continuous-monitor substrate (which previously
carried a near-identical private copy).

Each helper accepts a duck-typed object (annotated ``Any``) — the orchestrator
passes a ``ResolvedConfig`` instance, but unit tests can substitute a
minimal fake without importing the full config-resolver dependency chain.
A future schema change that renames or relocates ``assets`` /
``assets.sectors`` surfaces as a visible warning rather than silently
producing empty data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

__all__ = [
    "SectorResolver",
    "active_sectors_from_resolved",
    "build_sector_resolver",
    "sectors_config_from_assets",
    "ticker_scope_from_assets",
]

log = logging.getLogger(__name__)


def active_sectors_from_resolved(resolved: Any) -> set[str]:
    """Read active sectors from the loaded assets config.

    Logs a warning and returns an empty set when the resolved config lacks
    either ``assets`` or ``assets.sectors``. Future schema changes that
    rename or relocate these attributes surface as a visible warning rather
    than silently producing empty data.
    """
    if not hasattr(resolved, "assets"):
        log.warning("resolved config has no 'assets' attribute; active_sectors empty")
        return set()
    if not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets has no 'sectors' attribute; active_sectors empty")
        return set()
    return set(resolved.assets.sectors.keys())


class SectorResolver:
    """Callable ticker→sector resolver backed by a static lookup dict.

    Module-level class (not a closure) so pickle can serialize instances
    across a subprocess boundary — the ``_sdk_subprocess`` wrappers pickle
    ``ValidationToolState`` whose ``sector_resolver`` field carries one of
    these instances.
    """

    __slots__ = ("ticker_to_sector",)

    def __init__(self, ticker_to_sector: dict[str, str]) -> None:
        self.ticker_to_sector = ticker_to_sector

    def __call__(self, ticker: str) -> str:
        return self.ticker_to_sector.get(ticker, "UNCLASSIFIED")


def build_sector_resolver(resolved: Any) -> Callable[[str], str]:
    """Build a ticker→sector resolver from the resolved assets config.

    Walks ``resolved.assets.sectors`` (``dict[sector, list[ticker]]``) and
    constructs the inverse map. The returned callable looks up the ticker
    and returns its sector; tickers absent from every sector list resolve
    to ``"UNCLASSIFIED"`` (the same sentinel
    :func:`alphamind.portfolio_state.consumers.synthesizer._project_positions`
    uses). Logs a warning when ``assets.sectors`` is unavailable.
    """
    ticker_to_sector: dict[str, str] = {}
    if not hasattr(resolved, "assets") or not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets.sectors unavailable; sector_resolver returns 'UNCLASSIFIED'")
    else:
        for sector, tickers in resolved.assets.sectors.items():
            for ticker in tickers:
                ticker_to_sector[ticker] = sector

    return SectorResolver(ticker_to_sector)


def ticker_scope_from_assets(resolved: Any) -> tuple[str, ...]:
    """Extract the per-invocation ticker scope from the resolved assets config.

    ``AssetsConfig`` does not expose a top-level ``universe`` field; the
    scope is the alphabetized union of every sector's tickers (mirroring
    :func:`alphamind.scripts._common.load_universe_scope`). Logs a warning
    and returns an empty tuple when ``resolved`` lacks an ``assets`` /
    ``assets.sectors`` attribute path — future schema changes surface as a
    visible warning rather than silent empty data.
    """
    if not hasattr(resolved, "assets"):
        log.warning("resolved config has no 'assets' attribute; ticker_scope empty")
        return ()
    if not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets has no 'sectors' attribute; ticker_scope empty")
        return ()
    tickers: set[str] = set()
    for sector_tickers in resolved.assets.sectors.values():
        tickers.update(sector_tickers)
    return tuple(sorted(tickers))


def sectors_config_from_assets(resolved: Any) -> dict[str, list[str]]:
    """Project ``resolved.assets.sectors`` into the analysis-layer sector map.

    ``assets.yaml`` keeps ``tech`` and ``semis`` separate so each can carry
    its own discovery-source ETF (XLK / SOXX). The analysis layer collapses
    them into a single ``tech_semis`` researcher (matches the ``Sector``
    enum and the ``sector_classification.domain_researcher`` storage value),
    so this helper merges the two raw buckets and emits exactly the three
    canonical keys: ``tech_semis`` / ``financials`` / ``energy``.

    Logs a warning and returns an empty dict when the resolved config
    lacks ``assets`` / ``assets.sectors``.
    """
    if not hasattr(resolved, "assets"):
        log.warning("resolved config has no 'assets' attribute; sectors_config empty")
        return {}
    if not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets has no 'sectors' attribute; sectors_config empty")
        return {}
    raw = {sector: list(tickers) for sector, tickers in resolved.assets.sectors.items()}
    return {
        "tech_semis": raw.get("tech", []) + raw.get("semis", []),
        "financials": raw.get("financials", []),
        "energy": raw.get("energy", []),
    }
