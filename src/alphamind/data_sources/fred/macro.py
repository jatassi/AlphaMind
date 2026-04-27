"""
FRED macro time-series collector.

Pulls FRED observations for the configured series list and writes rows
to ``macro_observations``.

Public API
----------
- :func:`collect_series` — incremental collect for a given series list + since date.
- :func:`bootstrap_series` — one-shot bootstrap (90d daily / 24m monthly).

Revision detection
------------------
After fetching fresh observations, the collector queries the most recent stored
row for each ``(series_id, observation_date)`` pair.  If the new value differs
from the stored value, a new row with ``revision_number + 1`` is inserted.

Idempotency
-----------
The primary key on ``macro_observations`` is
``(source, series_id, observation_date, revision_number)``.
Rows are inserted only when they are genuinely new (either new dates or
new revision values), so re-running on the same window produces no duplicates.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select

from alphamind.data_sources._common import track_run
from alphamind.data_sources.fred.client import FredClient
from alphamind.data_sources.fred.series import DAILY_SERIES, MONTHLY_SERIES
from alphamind.persistence.models import MacroObservations

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE = "fred"
_COLLECTOR_NAME = "fred.macro"

# Bootstrap lookback depths (from lifecycle.md)
_DAILY_LOOKBACK_DAYS = 90
_MONTHLY_LOOKBACK_YEARS = 2

# ---------------------------------------------------------------------------
# Units normalization table
# ---------------------------------------------------------------------------
# Maps substrings in FRED's verbose units string → storage schema short form.
# Checked in order; first match wins.

_UNITS_MAP: list[tuple[str, str]] = [
    ("Basis Points", "bp"),
    ("Dollars per Barrel", "bbl"),
    ("Dollars per Gallon", "bbl"),
    ("Billions of Dollars", "usd"),
    ("Millions of Dollars", "usd"),
    ("Trillions of Dollars", "usd"),
    ("Dollars", "usd"),
    ("Percent", "pct"),
    ("Index", "index"),
    ("Thousands", "units"),
    ("Millions", "units"),
    ("Billions", "units"),
]


def _normalize_units(fred_units: str) -> str:
    """Return the storage-schema short form for a FRED units string."""
    for fragment, short in _UNITS_MAP:
        if fragment.lower() in fred_units.lower():
            return short
    return "units"


# ---------------------------------------------------------------------------
# Frequency normalization table
# ---------------------------------------------------------------------------

_FREQ_MAP: dict[str, str] = {
    "D": "daily",
    "W": "weekly",
    "BW": "weekly",
    "M": "monthly",
    "Q": "quarterly",
    "SA": "annual",
    "A": "annual",
}


def _normalize_frequency(freq_short: str) -> str:
    """Return the storage-schema frequency label for a FRED frequency_short code."""
    return _FREQ_MAP.get(freq_short, freq_short.lower())


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _values_differ(a: float | None, b: float | None) -> bool:
    """Return True when two float values are meaningfully different."""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _bulk_fetch_latest(
    session_factory: Any,
    series_id: str,
    observation_dates: list[str],
) -> dict[str, MacroObservations]:
    """
    Return the highest-revision stored row for each date in *observation_dates*.

    One query per series window instead of one per observation date.
    """
    if not observation_dates:
        return {}
    with session_factory() as sess:
        rows = (
            sess.execute(
                select(MacroObservations)
                .where(
                    MacroObservations.source == _SOURCE,
                    MacroObservations.series_id == series_id,
                    MacroObservations.observation_date.in_(observation_dates),
                )
                .order_by(MacroObservations.revision_number.desc())
            )
            .scalars()
            .all()
        )
    # Keep only the highest revision per date
    latest: dict[str, MacroObservations] = {}
    for row in rows:
        if row.observation_date not in latest:
            latest[row.observation_date] = row
    return latest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_series(
    series_ids: list[str],
    since: date,
    *,
    client: FredClient | None = None,
    session_factory: Any = None,
    _repo: Any = None,
) -> int:
    """
    Pull FRED observations for *series_ids* since *since* and write to DB.

    Parameters
    ----------
    series_ids:
        List of FRED series IDs to collect.
    since:
        Earliest observation date to request.
    client:
        Injectable :class:`~alphamind.data_sources.fred.client.FredClient`.
        When *None*, a default client is constructed using config.
    session_factory:
        Injectable SQLAlchemy session factory.  When *None*, the production
        session factory is used.
    _repo:
        Injectable ``track_run`` repo for testing.

    Returns
    -------
    int
        Number of new rows written.
    """
    if client is None:
        client = FredClient()  # fredapi resolves FRED_API_KEY from env

    if session_factory is None:
        from alphamind.persistence.models import Base
        from alphamind.persistence.session import make_engine, make_session_factory

        engine = make_engine()
        Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)

    with track_run(_COLLECTOR_NAME, _repo=_repo) as run:
        rows_written = 0

        for series_id in series_ids:
            info = client.get_series_info(series_id)
            frequency = _normalize_frequency(str(info.get("frequency_short", "")))
            units = _normalize_units(str(info.get("units", "")))

            observations: pd.Series = client.get_series(
                series_id,
                observation_start=since,
            )

            if observations is None or len(observations) == 0:
                continue

            incoming: list[tuple[str, float | None]] = [
                (
                    pd.Timestamp(obs_ts).date().isoformat(),
                    float(value) if pd.notna(value) else None,
                )
                for obs_ts, value in observations.items()
            ]

            obs_dates = [obs_date for obs_date, _ in incoming]
            existing_by_date = _bulk_fetch_latest(session_factory, series_id, obs_dates)

            ingested_at = datetime.now(UTC).isoformat()
            new_rows: list[MacroObservations] = []
            for obs_date, float_value in incoming:
                existing = existing_by_date.get(obs_date)
                if existing is not None and not _values_differ(existing.value, float_value):
                    continue
                revision_number = 0 if existing is None else existing.revision_number + 1
                new_rows.append(
                    MacroObservations(
                        source=_SOURCE,
                        series_id=series_id,
                        observation_date=obs_date,
                        revision_number=revision_number,
                        value=float_value,
                        units=units,
                        frequency=frequency,
                        ingested_at=ingested_at,
                    )
                )

            if new_rows:
                with session_factory() as sess:
                    sess.add_all(new_rows)
                    sess.commit()
                rows_written += len(new_rows)

        run.rows_written = rows_written

    return rows_written


def bootstrap_series(
    *,
    daily_series: list[str] | None = None,
    monthly_series: list[str] | None = None,
    client: FredClient | None = None,
    session_factory: Any = None,
    _repo: Any = None,
) -> int:
    """
    One-shot bootstrap: 90 days for daily series, 24 months for monthly series.

    Parameters
    ----------
    daily_series:
        Override the default :data:`~alphamind.data_sources.fred.series.DAILY_SERIES` list.
    monthly_series:
        Override the default :data:`~alphamind.data_sources.fred.series.MONTHLY_SERIES` list.
    client:
        Injectable :class:`~.client.FredClient`.
    session_factory:
        Injectable session factory.
    _repo:
        Injectable track_run repo.

    Returns
    -------
    int
        Total new rows written across all series.
    """
    today = date.today()
    daily_since = today - timedelta(days=_DAILY_LOOKBACK_DAYS)
    monthly_since = date(today.year - _MONTHLY_LOOKBACK_YEARS, today.month, 1)

    effective_daily = daily_series if daily_series is not None else DAILY_SERIES
    effective_monthly = monthly_series if monthly_series is not None else MONTHLY_SERIES

    if client is None:
        client = FredClient()

    total = 0
    total += collect_series(
        effective_daily,
        since=daily_since,
        client=client,
        session_factory=session_factory,
        _repo=_repo,
    )
    total += collect_series(
        effective_monthly,
        since=monthly_since,
        client=client,
        session_factory=session_factory,
        _repo=_repo,
    )
    return total
