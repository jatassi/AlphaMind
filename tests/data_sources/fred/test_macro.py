"""Tests for src/alphamind/data_sources/fred/macro.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alphamind.persistence.models import Base, CollectionRuns, MacroObservations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite database with all tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def mock_repo(engine):
    """
    A track_run _repo compatible with the in-memory DB.
    Mirrors _DefaultRepo from _common.py but points at the test engine.
    """
    from alphamind.persistence.models import CollectionRuns
    from alphamind.persistence.session import make_session_factory

    _sf = make_session_factory(engine)

    class Repo:
        def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
            with _sf() as sess:
                sess.add(
                    CollectionRuns(
                        run_id=run_id,
                        collector=collector,
                        started_at=started_at,
                        status="running",
                    )
                )
                sess.commit()

        def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
            with _sf() as sess:
                row = sess.get(CollectionRuns, run_id)
                if row:
                    row.status = "success"
                    row.completed_at = completed_at
                    row.rows_written = rows_written
                    sess.commit()

        def update_failed(self, run_id: str, error_summary: str) -> None:
            with _sf() as sess:
                row = sess.get(CollectionRuns, run_id)
                if row:
                    row.status = "failed"
                    row.error_summary = error_summary
                    sess.commit()

    return Repo()


def _make_series_data(dates: list[date], values: list[float]) -> pd.Series:
    """Build a pandas Series that mimics fredapi.Fred.get_series() output."""
    return pd.Series(
        data=values,
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates]),
    )


def _make_series_info(frequency_short: str = "D", units: str = "Percent") -> pd.Series:
    """Build a pandas Series that mimics fredapi.Fred.get_series_info() output."""
    return pd.Series(
        {
            "id": "DGS10",
            "title": "10-Year Treasury",
            "frequency_short": frequency_short,
            "units": units,
            "observation_start": "2000-01-01",
            "observation_end": "2026-04-26",
        }
    )


# ---------------------------------------------------------------------------
# collect_series — writes rows
# ---------------------------------------------------------------------------


def test_collect_series_writes_macro_observations(engine, session_factory, mock_repo) -> None:
    """collect_series() writes one row per observation date."""
    from alphamind.data_sources.fred.macro import collect_series

    obs_dates = [date(2026, 4, 24), date(2026, 4, 25)]
    series_data = _make_series_data(obs_dates, [4.25, 4.30])
    series_info = _make_series_info(frequency_short="D", units="Percent")

    mock_client = MagicMock()
    mock_client.get_series.return_value = series_data
    mock_client.get_series_info.return_value = series_info

    collect_series(
        ["DGS10"],
        since=date(2026, 4, 24),
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    with session_factory() as sess:
        rows = sess.query(MacroObservations).all()

    assert len(rows) == 2
    assert all(r.source == "fred" for r in rows)
    assert all(r.series_id == "DGS10" for r in rows)
    assert {r.observation_date for r in rows} == {"2026-04-24", "2026-04-25"}
    assert all(r.revision_number == 0 for r in rows)


# ---------------------------------------------------------------------------
# collect_series — frequency from metadata
# ---------------------------------------------------------------------------


def test_collect_series_sets_frequency_from_metadata(engine, session_factory, mock_repo) -> None:
    """macro_observations.frequency is populated from FRED metadata."""
    from alphamind.data_sources.fred.macro import collect_series

    series_data = _make_series_data([date(2026, 4, 25)], [4.25])
    series_info = _make_series_info(frequency_short="M", units="Percent")

    mock_client = MagicMock()
    mock_client.get_series.return_value = series_data
    mock_client.get_series_info.return_value = series_info

    collect_series(
        ["CPIAUCSL"],
        since=date(2026, 4, 1),
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    with session_factory() as sess:
        row = sess.query(MacroObservations).first()

    assert row is not None
    assert row.frequency == "monthly"


# ---------------------------------------------------------------------------
# collect_series — units normalized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fred_units,expected_short",
    [
        ("Percent", "pct"),
        ("Percent Change", "pct"),
        ("Basis Points", "bp"),
        ("Index", "index"),
        ("Dollars per Barrel", "bbl"),
        ("Billions of Dollars", "usd"),
        ("Millions of Dollars", "usd"),
        ("Thousands", "units"),
        ("Index 1982-84=100", "index"),
    ],
)
def test_collect_series_normalizes_units(
    fred_units: str,
    expected_short: str,
    engine: object,
    session_factory: object,
    mock_repo: object,
) -> None:
    """macro_observations.units is normalized from verbose FRED units string."""
    from alphamind.data_sources.fred.macro import collect_series

    series_data = _make_series_data([date(2026, 4, 25)], [1.0])
    series_info = _make_series_info(frequency_short="D", units=fred_units)

    mock_client = MagicMock()
    mock_client.get_series.return_value = series_data
    mock_client.get_series_info.return_value = series_info

    collect_series(
        ["TEST"],
        since=date(2026, 4, 25),
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    with session_factory() as sess:
        row = sess.query(MacroObservations).first()

    assert row is not None
    assert row.units == expected_short


# ---------------------------------------------------------------------------
# collect_series — revision detection
# ---------------------------------------------------------------------------


def test_collect_series_inserts_revision_when_value_differs(
    engine, session_factory, mock_repo
) -> None:
    """When a stored row's value differs, a new row with revision_number+1 is inserted."""
    from alphamind.data_sources.fred.macro import collect_series

    # Seed an existing row at revision 0
    with session_factory() as sess:
        sess.add(
            MacroObservations(
                source="fred",
                series_id="DGS10",
                observation_date="2026-04-24",
                revision_number=0,
                value=4.20,
                units="pct",
                frequency="daily",
                ingested_at="2026-04-24T00:00:00+00:00",
            )
        )
        sess.commit()

    # New pull returns a different value for the same date
    series_data = _make_series_data([date(2026, 4, 24)], [4.25])
    series_info = _make_series_info(frequency_short="D", units="Percent")

    mock_client = MagicMock()
    mock_client.get_series.return_value = series_data
    mock_client.get_series_info.return_value = series_info

    collect_series(
        ["DGS10"],
        since=date(2026, 4, 24),
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    with session_factory() as sess:
        rows = (
            sess.query(MacroObservations)
            .filter_by(series_id="DGS10", observation_date="2026-04-24")
            .order_by(MacroObservations.revision_number)
            .all()
        )

    assert len(rows) == 2
    assert rows[0].revision_number == 0
    assert rows[0].value == pytest.approx(4.20)
    assert rows[1].revision_number == 1
    assert rows[1].value == pytest.approx(4.25)


def test_collect_series_no_revision_when_value_same(engine, session_factory, mock_repo) -> None:
    """When the stored value matches the new pull, no revision row is inserted."""
    from alphamind.data_sources.fred.macro import collect_series

    with session_factory() as sess:
        sess.add(
            MacroObservations(
                source="fred",
                series_id="DGS10",
                observation_date="2026-04-24",
                revision_number=0,
                value=4.25,
                units="pct",
                frequency="daily",
                ingested_at="2026-04-24T00:00:00+00:00",
            )
        )
        sess.commit()

    series_data = _make_series_data([date(2026, 4, 24)], [4.25])
    series_info = _make_series_info(frequency_short="D", units="Percent")

    mock_client = MagicMock()
    mock_client.get_series.return_value = series_data
    mock_client.get_series_info.return_value = series_info

    collect_series(
        ["DGS10"],
        since=date(2026, 4, 24),
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    with session_factory() as sess:
        rows = (
            sess.query(MacroObservations)
            .filter_by(series_id="DGS10", observation_date="2026-04-24")
            .all()
        )

    assert len(rows) == 1


# ---------------------------------------------------------------------------
# collect_series — idempotency
# ---------------------------------------------------------------------------


def test_collect_series_idempotent_on_same_window(engine, session_factory, mock_repo) -> None:
    """Re-running collect_series on the same window produces no duplicate rows."""
    from alphamind.data_sources.fred.macro import collect_series

    obs_dates = [date(2026, 4, 24), date(2026, 4, 25)]
    series_data = _make_series_data(obs_dates, [4.25, 4.30])
    series_info = _make_series_info(frequency_short="D", units="Percent")

    mock_client = MagicMock()
    mock_client.get_series.return_value = series_data
    mock_client.get_series_info.return_value = series_info

    kwargs = dict(
        series_ids=["DGS10"],
        since=date(2026, 4, 24),
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    collect_series(**kwargs)

    # Reset mock to return same data
    mock_client.get_series.return_value = series_data
    mock_client.get_series_info.return_value = series_info
    collect_series(**kwargs)

    with session_factory() as sess:
        rows = sess.query(MacroObservations).all()

    # Exactly 2 rows (one per date, no duplicates)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# collect_series — full failure records failed status
# ---------------------------------------------------------------------------


def test_collect_series_records_failed_on_full_error(engine, session_factory, mock_repo) -> None:
    """On full failure, collection_runs records 'failed' and no data rows are written."""
    from alphamind.data_sources.fred.macro import collect_series

    mock_client = MagicMock()
    mock_client.get_series.side_effect = RuntimeError("FRED is down")
    mock_client.get_series_info.side_effect = RuntimeError("FRED is down")

    with pytest.raises(RuntimeError, match="FRED is down"):
        collect_series(
            ["DGS10"],
            since=date(2026, 4, 24),
            client=mock_client,
            session_factory=session_factory,
            _repo=mock_repo,
        )

    with session_factory() as sess:
        run = sess.query(CollectionRuns).first()
        macro_rows = sess.query(MacroObservations).all()

    assert run is not None
    assert run.status == "failed"
    assert len(macro_rows) == 0


# ---------------------------------------------------------------------------
# bootstrap_series — depth
# ---------------------------------------------------------------------------


def test_collect_series_callable_with_no_args(engine, session_factory, mock_repo) -> None:
    """collect_series() is callable with no positional args (cron registry contract)."""
    from alphamind.data_sources.fred.macro import collect_series

    mock_client = MagicMock()
    mock_client.get_series.return_value = pd.Series(dtype=float)
    mock_client.get_series_info.return_value = _make_series_info("D", "Percent")

    # No series_ids, no since — must not raise
    collect_series(
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )


# ---------------------------------------------------------------------------
# bootstrap_series — depth
# ---------------------------------------------------------------------------


def test_bootstrap_series_uses_90_days_for_daily(engine, session_factory, mock_repo) -> None:
    """bootstrap_series() requests ~90 days of history for daily series."""
    from alphamind.data_sources.fred.macro import (
        _DAILY_LOOKBACK_DAYS,
        bootstrap_series,
    )
    from alphamind.data_sources.fred.series import DAILY_SERIES

    mock_client = MagicMock()
    mock_client.get_series.return_value = pd.Series(dtype=float)
    mock_client.get_series_info.return_value = _make_series_info("D", "Percent")

    bootstrap_series(
        daily_series=DAILY_SERIES[:1],  # only one series to keep the test fast
        monthly_series=[],
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    calls = mock_client.get_series.call_args_list
    assert len(calls) == 1
    obs_start = calls[0].kwargs.get("observation_start")
    assert obs_start is not None
    # The since date should be ~90 days in the past (within a 1-day tolerance)
    expected = datetime.now(UTC).date() - timedelta(days=_DAILY_LOOKBACK_DAYS)
    assert abs((obs_start - expected).days) <= 1


def test_bootstrap_series_uses_24_months_for_monthly(engine, session_factory, mock_repo) -> None:
    """bootstrap_series() requests ~24 months of history for monthly series."""
    from alphamind.data_sources.fred.macro import bootstrap_series
    from alphamind.data_sources.fred.series import MONTHLY_SERIES

    mock_client = MagicMock()
    mock_client.get_series.return_value = pd.Series(dtype=float)
    mock_client.get_series_info.return_value = _make_series_info("M", "Percent")

    bootstrap_series(
        daily_series=[],
        monthly_series=MONTHLY_SERIES[:1],
        client=mock_client,
        session_factory=session_factory,
        _repo=mock_repo,
    )

    calls = mock_client.get_series.call_args_list
    assert len(calls) == 1
    obs_start = calls[0].kwargs.get("observation_start")
    assert obs_start is not None

    # 24 months back: obs_start should be around 2 years ago
    today = datetime.now(UTC).date()
    expected_year = today.year - 2
    expected_month = today.month
    expected = date(expected_year, expected_month, 1)
    # Allow a couple of days tolerance
    assert abs((obs_start - expected).days) <= 3
