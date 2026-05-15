"""Tests for ``alphamind.config.assets_views``.

These four helpers unpack ``resolved.assets.sectors`` into typed views the
analysis / decision pipelines consume — sector-set, ticker-scope, sectors
dict, ticker-to-sector resolver. They lived inline in
``scheduler/orchestrator.py`` until ALP-472; the lift homes them in the
config layer they project from.
"""

from __future__ import annotations

import logging
from typing import Any


class _FakeAssets:
    def __init__(self, sectors: dict[str, list[str]]):
        self.sectors = sectors


class _FakeResolved:
    def __init__(self, assets: Any) -> None:
        self.assets = assets


def test_active_sectors_from_resolved_returns_keys() -> None:
    """Returns the set of sector keys from ``resolved.assets.sectors``."""
    from alphamind.config.assets_views import active_sectors_from_resolved

    resolved = _FakeResolved(_FakeAssets({"TECH": ["AAPL"], "ENERGY": ["XOM"]}))
    assert active_sectors_from_resolved(resolved) == {"TECH", "ENERGY"}


def test_active_sectors_from_resolved_missing_assets_returns_empty(
    caplog: Any,
) -> None:
    """Missing ``assets`` attribute logs a warning and returns an empty set."""
    from alphamind.config.assets_views import active_sectors_from_resolved

    class _NoAssets:
        pass

    with caplog.at_level(logging.WARNING):
        result = active_sectors_from_resolved(_NoAssets())

    assert result == set()
    assert any("assets" in record.message for record in caplog.records)


def test_active_sectors_from_resolved_missing_sectors_returns_empty(
    caplog: Any,
) -> None:
    """Missing ``assets.sectors`` attribute logs a warning and returns empty."""
    from alphamind.config.assets_views import active_sectors_from_resolved

    class _NoSectors:
        pass

    resolved = _FakeResolved(_NoSectors())
    with caplog.at_level(logging.WARNING):
        result = active_sectors_from_resolved(resolved)

    assert result == set()
    assert any("sectors" in record.message for record in caplog.records)


def test_build_sector_resolver_returns_inverse_map() -> None:
    """Resolver maps each ticker to its containing sector key."""
    from alphamind.config.assets_views import build_sector_resolver

    resolved = _FakeResolved(_FakeAssets({"TECH": ["AAPL", "MSFT"], "ENERGY": ["XOM"]}))
    resolver = build_sector_resolver(resolved)

    assert resolver("AAPL") == "TECH"
    assert resolver("MSFT") == "TECH"
    assert resolver("XOM") == "ENERGY"


def test_build_sector_resolver_unknown_ticker_returns_unclassified() -> None:
    """An unknown ticker resolves to ``"UNCLASSIFIED"`` sentinel."""
    from alphamind.config.assets_views import build_sector_resolver

    resolved = _FakeResolved(_FakeAssets({"TECH": ["AAPL"]}))
    resolver = build_sector_resolver(resolved)

    assert resolver("UNKNOWN") == "UNCLASSIFIED"


def test_build_sector_resolver_missing_sectors_returns_unclassified(
    caplog: Any,
) -> None:
    """Missing ``assets.sectors`` produces an always-``UNCLASSIFIED`` resolver."""
    from alphamind.config.assets_views import build_sector_resolver

    class _NoAssets:
        pass

    with caplog.at_level(logging.WARNING):
        resolver = build_sector_resolver(_NoAssets())

    assert resolver("AAPL") == "UNCLASSIFIED"
    assert any("UNCLASSIFIED" in record.message for record in caplog.records)


def test_ticker_scope_from_assets_returns_sorted_union() -> None:
    """``ticker_scope`` is the alphabetized union of every sector's tickers."""
    from alphamind.config.assets_views import ticker_scope_from_assets

    resolved = _FakeResolved(_FakeAssets({"TECH": ["MSFT", "AAPL"], "ENERGY": ["XOM"]}))
    assert ticker_scope_from_assets(resolved) == ("AAPL", "MSFT", "XOM")


def test_ticker_scope_from_assets_missing_returns_empty(caplog: Any) -> None:
    """Missing ``assets``/``assets.sectors`` returns an empty tuple with warning."""
    from alphamind.config.assets_views import ticker_scope_from_assets

    class _NoAssets:
        pass

    with caplog.at_level(logging.WARNING):
        result = ticker_scope_from_assets(_NoAssets())

    assert result == ()
    assert caplog.records  # at least one warning


def test_sectors_config_from_assets_returns_dict() -> None:
    """``sectors_config_from_assets`` returns a mutable copy of the sector map."""
    from alphamind.config.assets_views import sectors_config_from_assets

    sectors = {"TECH": ["AAPL", "MSFT"], "ENERGY": ["XOM"]}
    resolved = _FakeResolved(_FakeAssets(sectors))

    result = sectors_config_from_assets(resolved)

    assert result == {"TECH": ["AAPL", "MSFT"], "ENERGY": ["XOM"]}


def test_sectors_config_from_assets_missing_returns_empty(caplog: Any) -> None:
    """Missing ``assets``/``assets.sectors`` returns an empty dict with warning."""
    from alphamind.config.assets_views import sectors_config_from_assets

    class _NoAssets:
        pass

    with caplog.at_level(logging.WARNING):
        result = sectors_config_from_assets(_NoAssets())

    assert result == {}
    assert caplog.records
