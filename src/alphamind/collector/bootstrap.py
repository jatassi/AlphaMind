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

import yaml

from alphamind.data_sources.bls.macro import bootstrap_series as bls_bootstrap_series
from alphamind.data_sources.eia.energy import bootstrap_series as eia_bootstrap_series
from alphamind.data_sources.finnhub.calendar import (
    bootstrap_earnings_calendar,
    bootstrap_economic_calendar,
    collect_fda_calendar,
    collect_ipo_calendar,
)
from alphamind.data_sources.finnhub.estimate_revisions import bootstrap_estimate_revisions
from alphamind.data_sources.finra.short_interest import bootstrap_short_interest
from alphamind.data_sources.finra.short_volume import bootstrap_short_volume
from alphamind.data_sources.polygon.corporate_actions import bootstrap_corporate_actions
from alphamind.data_sources.polygon.equity import bootstrap_universe_bars
from alphamind.data_sources.polygon.reference import collect_reference
from alphamind.data_sources.treasury.auctions import bootstrap_auctions

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known vendor names for --only dispatch validation
# ---------------------------------------------------------------------------

_KNOWN_VENDORS: frozenset[str] = frozenset(
    {"polygon", "fred", "eia", "treasury", "bls", "finnhub", "finra"}
)

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
    with assets_path.open(encoding="utf-8") as fh:
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
        # Maps assets.yaml sector key → distillation/analysis-layer
        # ``sector_classification.domain_researcher`` value. Tech and semis
        # share one researcher per the analysis-layer design.
        _domain_researcher_map = {
            "tech": "tech_semis",
            "semis": "tech_semis",
            "financials": "financials",
            "energy": "energy",
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
                            domain_researcher=_domain_researcher_map.get(sector_name, ""),
                            sector_etf=_sector_etf_map.get(sector_name, ""),
                            classification_source="assets.yaml",
                            last_updated=now,
                        )
                    )
                    rows_written += 1
                else:
                    # Idempotent backfill: earlier bootstraps wrote
                    # ``domain_researcher=""`` (the column was added without
                    # a populated value). Refresh it from the current map so
                    # re-running bootstrap repairs the gap without manual
                    # SQL.
                    classification = (
                        sess.query(SectorClassification).filter_by(ticker=ticker).first()
                    )
                    desired_researcher = _domain_researcher_map.get(sector_name, "")
                    if (
                        classification is not None
                        and classification.domain_researcher != desired_researcher
                    ):
                        classification.domain_researcher = desired_researcher
                        classification.last_updated = now
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
        # Per-step supervisor per runtime §G1: per the lifecycle.md interrupt-
        # safe guarantee, one step's failure must not abort the rest of the
        # plan. ``BaseException`` (``KeyboardInterrupt``) propagates so the
        # operator can cancel the bootstrap.
        elapsed = time.monotonic() - t0
        log.exception("bootstrap: %s — failed after %.1fs", label, elapsed)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _bootstrap_plan() -> list[tuple[str, list[tuple[str, Any]]]]:
    """Return the ordered bootstrap plan, evaluated at call time.

    Evaluating lazily ensures that ``unittest.mock.patch`` applied to any
    module-level name in this module takes effect when ``run_all`` runs.
    """
    return [
        # Steps 1-3 — Polygon: seed -> reference -> corporate actions -> equity
        (
            "polygon",
            [
                ("assets.yaml seed", _seed_asset_universe),
                ("polygon.reference", collect_reference),
                ("polygon.corporate_actions", bootstrap_corporate_actions),
                ("polygon.equity", bootstrap_universe_bars),
            ],
        ),
        # Step 4 — Macro daily: FRED daily, EIA, Treasury
        ("fred", [("fred.daily", _fred_bootstrap_daily)]),
        ("eia", [("eia", eia_bootstrap_series)]),
        ("treasury", [("treasury", bootstrap_auctions)]),
        # Step 5 — Macro monthly: FRED monthly, BLS
        ("fred", [("fred.monthly", _fred_bootstrap_monthly)]),
        ("bls", [("bls", bls_bootstrap_series)]),
        # Step 4b — Short selling: FINRA (no dependency on Polygon equity bootstrap)
        (
            "finra",
            [
                ("finra.short_volume", bootstrap_short_volume),
                ("finra.short_interest", bootstrap_short_interest),
            ],
        ),
        # Step 6 — Event + earnings calendar (Finnhub)
        # IPO and FDA calendars have no dedicated bootstrap wrappers; the
        # catch-up paths resolve a forward-90d window themselves when called
        # with no ``since``. lifecycle.md § Bootstrap step 6 includes them.
        (
            "finnhub",
            [
                ("finnhub.earnings_calendar", bootstrap_earnings_calendar),
                ("finnhub.economic_calendar", bootstrap_economic_calendar),
                ("finnhub.ipo_calendar", collect_ipo_calendar),
                ("finnhub.fda_calendar", collect_fda_calendar),
                ("finnhub.estimate_revisions", bootstrap_estimate_revisions),
            ],
        ),
    ]


def run_all(only_vendor: str | None = None) -> None:
    """
    Orchestrate the full one-time bootstrap or a single vendor's bootstrap.

    Parameters
    ----------
    only_vendor:
        When provided, run only that vendor's bootstrap steps.  Accepted
        values: ``"polygon"``, ``"fred"``, ``"eia"``, ``"treasury"``,
        ``"bls"``, ``"finnhub"``, ``"finra"``.  ``None`` runs all vendors in order.

    Raises
    ------
    ValueError
        If ``only_vendor`` is not a recognised vendor name.
    """
    if only_vendor is not None and only_vendor not in _KNOWN_VENDORS:
        raise ValueError(f"unknown vendor {only_vendor!r}; valid values: {sorted(_KNOWN_VENDORS)}")

    log.info("bootstrap: starting (only_vendor=%r)", only_vendor)

    for vendor, steps in _bootstrap_plan():
        if only_vendor is None or only_vendor == vendor:
            for label, fn in steps:
                _run_step(label, fn)

    log.info("bootstrap: complete")
