"""
Bootstrap orchestrator for AlphaMind's data layer.

Calls each vendor's ``bootstrap_*()`` function in the order specified by
``docs/design/01-data-layer/collector/lifecycle.md § Bootstrap``:

  1. Reference: seed ``asset_universe`` from ``assets.yaml``, then
     ``polygon.reference.collect_reference`` enriches with live data.
  2. Corporate actions: ``polygon.corporate_actions.bootstrap_corporate_actions``.
  3. Equity OHLCV: ``polygon.equity.bootstrap_universe_bars``.
  4. Macro daily: ``fred.macro.bootstrap_series`` (daily only),
     ``eia.energy.bootstrap_series``, ``treasury.auctions.bootstrap_auctions``.
  5. Macro monthly: ``fred.macro.bootstrap_series`` (monthly only),
     ``bls.macro.bootstrap_series``.
  6. Event calendar: ``finnhub.calendar.bootstrap_earnings_calendar``,
     ``bootstrap_economic_calendar``.

Public interface
----------------
- ``run_all(only_vendor=None)`` — orchestrate all steps or a single vendor.

The function is callable from a Python REPL as well as from
``src/alphamind/collector/__main__.py``.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from alphamind.data_sources.bls.macro import bootstrap_series as bls_bootstrap_series
from alphamind.data_sources.eia.energy import bootstrap_series as eia_bootstrap_series
from alphamind.data_sources.finnhub.calendar import (
    bootstrap_earnings_calendar,
    bootstrap_economic_calendar,
)
from alphamind.data_sources.polygon.corporate_actions import bootstrap_corporate_actions
from alphamind.data_sources.polygon.equity import bootstrap_universe_bars
from alphamind.data_sources.polygon.reference import collect_reference
from alphamind.data_sources.treasury.auctions import bootstrap_auctions

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known vendor names for --only dispatch validation
# ---------------------------------------------------------------------------

_KNOWN_VENDORS: frozenset[str] = frozenset({"polygon", "fred", "eia", "treasury", "bls", "finnhub"})

# ---------------------------------------------------------------------------
# FRED daily / monthly wrappers
# ---------------------------------------------------------------------------


def _fred_bootstrap_daily() -> None:
    """Bootstrap FRED daily series (90-day lookback)."""
    from alphamind.data_sources.fred.macro import bootstrap_series as fred_bootstrap_series
    from alphamind.data_sources.fred.series import DAILY_SERIES

    fred_bootstrap_series(daily_series=DAILY_SERIES, monthly_series=[])


def _fred_bootstrap_monthly() -> None:
    """Bootstrap FRED monthly series (24-month lookback)."""
    from alphamind.data_sources.fred.macro import bootstrap_series as fred_bootstrap_series
    from alphamind.data_sources.fred.series import MONTHLY_SERIES

    fred_bootstrap_series(daily_series=[], monthly_series=MONTHLY_SERIES)


# ---------------------------------------------------------------------------
# assets.yaml seed
# ---------------------------------------------------------------------------


def _seed_asset_universe() -> int:
    """
    Seed ``asset_universe`` and ``sector_classification`` from ``assets.yaml``.

    Inserts one row per ticker using UPSERT semantics (idempotent).  Universe
    tickers get ``asset_role='universe'``; benchmark tickers get their
    role from the YAML ``role`` field.

    Returns the number of rows written.
    """
    from alphamind.persistence.models import AssetUniverse, Base, SectorClassification
    from alphamind.persistence.session import make_engine, make_session_factory

    assets_path = Path(__file__).parents[3] / "config" / "assets.yaml"
    with assets_path.open() as fh:
        assets: dict[str, Any] = yaml.safe_load(fh) or {}

    engine = make_engine()
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    now = datetime.now(UTC).isoformat()
    rows_written = 0

    with session_factory() as sess:
        # Universe tickers from sectors block
        sectors: dict[str, list[str]] = assets.get("sectors", {})
        _sector_etf_map = {
            "tech": "XLK",
            "semis": "SMH",
            "financials": "XLF",
            "energy": "XLE",
        }

        for sector_name, tickers in sectors.items():
            for ticker in tickers:
                existing = sess.query(AssetUniverse).filter_by(ticker=ticker).first()
                if existing is None:
                    asset_id = str(uuid.uuid4())
                    sess.add(
                        AssetUniverse(
                            asset_id=asset_id,
                            ticker=ticker,
                            full_name=ticker,
                            asset_class="equity",
                            asset_role="universe",
                            exchange="",
                            is_active=1,
                            added_date=now[:10],
                            last_updated=now,
                        )
                    )
                    sess.add(
                        SectorClassification(
                            ticker=ticker,
                            asset_id=asset_id,
                            alphamind_sector=sector_name,
                            domain_researcher="",
                            sector_etf=_sector_etf_map.get(sector_name, ""),
                            classification_source="assets.yaml",
                            last_updated=now,
                        )
                    )
                    rows_written += 1

        # Benchmark tickers from benchmarks block
        benchmarks: dict[str, dict[str, str]] = assets.get("benchmarks", {})
        for ticker, meta in benchmarks.items():
            existing = sess.query(AssetUniverse).filter_by(ticker=ticker).first()
            if existing is None:
                sess.add(
                    AssetUniverse(
                        asset_id=str(uuid.uuid4()),
                        ticker=ticker,
                        full_name=meta.get("description", ticker),
                        asset_class="etf",
                        asset_role=meta.get("role", "benchmark"),
                        exchange="",
                        is_active=1,
                        added_date=now[:10],
                        last_updated=now,
                    )
                )
                rows_written += 1

        sess.commit()

    return rows_written


# ---------------------------------------------------------------------------
# Step execution helpers
# ---------------------------------------------------------------------------


def _run_step(label: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    """
    Call ``fn(*args, **kwargs)`` with per-step logging and error isolation.

    Failures are logged at ERROR level but do not propagate — subsequent steps
    continue, matching the interrupt-safe guarantee in lifecycle.md.
    """
    log.info("bootstrap: %s — start", label)
    t0 = time.monotonic()
    try:
        fn(*args, **kwargs)
        elapsed = time.monotonic() - t0
        log.info("bootstrap: %s — done (%.1fs)", label, elapsed)
    except Exception:
        elapsed = time.monotonic() - t0
        log.exception("bootstrap: %s — failed after %.1fs", label, elapsed)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_all(only_vendor: str | None = None) -> None:
    """
    Orchestrate the full one-time bootstrap or a single vendor's bootstrap.

    Parameters
    ----------
    only_vendor:
        When provided, run only that vendor's bootstrap steps.  Accepted
        values: ``"polygon"``, ``"fred"``, ``"eia"``, ``"treasury"``,
        ``"bls"``, ``"finnhub"``.  ``None`` runs all vendors in order.

    Raises
    ------
    ValueError
        If ``only_vendor`` is not a recognised vendor name.
    """
    if only_vendor is not None and only_vendor not in _KNOWN_VENDORS:
        raise ValueError(f"unknown vendor {only_vendor!r}; valid values: {sorted(_KNOWN_VENDORS)}")

    log.info("bootstrap: starting (only_vendor=%r)", only_vendor)

    def _runs(vendor: str) -> bool:
        return only_vendor is None or only_vendor == vendor

    # ------------------------------------------------------------------
    # Steps 1-3 - Polygon: seed -> reference -> corporate actions -> equity
    # ------------------------------------------------------------------
    if _runs("polygon"):
        _run_step("assets.yaml seed", _seed_asset_universe)
        _run_step("polygon.reference", collect_reference, [])
        _run_step("polygon.corporate_actions", bootstrap_corporate_actions)
        _run_step("polygon.equity", bootstrap_universe_bars)

    # ------------------------------------------------------------------
    # Step 4 — Macro daily: FRED daily, EIA, Treasury
    # ------------------------------------------------------------------
    if _runs("fred"):
        _run_step("fred.daily", _fred_bootstrap_daily)
    if _runs("eia"):
        _run_step("eia", eia_bootstrap_series)
    if _runs("treasury"):
        _run_step("treasury", bootstrap_auctions)

    # ------------------------------------------------------------------
    # Step 5 — Macro monthly: FRED monthly, BLS
    # ------------------------------------------------------------------
    if _runs("fred"):
        _run_step("fred.monthly", _fred_bootstrap_monthly)
    if _runs("bls"):
        _run_step("bls", bls_bootstrap_series)

    # ------------------------------------------------------------------
    # Step 6 — Event + earnings calendar (Finnhub)
    # ------------------------------------------------------------------
    if _runs("finnhub"):
        _run_step("finnhub.earnings_calendar", bootstrap_earnings_calendar)
        _run_step("finnhub.economic_calendar", bootstrap_economic_calendar)

    log.info("bootstrap: complete")
