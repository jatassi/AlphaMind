"""
Tests for bls/macro.py.

All HTTP calls are mocked.  DB uses an in-memory SQLite engine.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from alphamind.data_sources.bls.macro import bootstrap_series, collect_series
from alphamind.persistence.models import Base, CollectionRuns, MacroObservations
from alphamind.persistence.session import make_engine, make_session_factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    return make_session_factory(engine)


def _make_bls_response(series_id: str, observations: list[tuple[str, str, str]]) -> dict:
    """Build a fake BLS API response payload.

    observations: list of (year, period, value) triples.
    """
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": series_id,
                    "data": [
                        {"year": y, "period": p, "value": v, "footnotes": [{}]}
                        for y, p, v in observations
                    ],
                }
            ]
        },
    }


def _fake_client(responses: list[dict]) -> MagicMock:
    """Return a mock BLSClient whose post_timeseries consumes responses in order."""
    client = MagicMock()
    side_effects = []
    for resp in responses:
        side_effects.append(resp["Results"]["series"])
    client.post_timeseries.side_effect = side_effects
    return client


# ---------------------------------------------------------------------------
# AC: collect_series writes macro_observations rows
# ---------------------------------------------------------------------------


class TestCollectSeries:
    def test_writes_rows_for_each_observation(self, engine, session_factory) -> None:
        fake_client = _fake_client(
            [
                _make_bls_response(
                    "LNS14000000",
                    [("2024", "M01", "3.7"), ("2024", "M02", "3.9")],
                )
            ]
        )

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            rows_written = collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
            )

        with Session(engine) as sess:
            rows = sess.query(MacroObservations).filter_by(series_id="LNS14000000").all()
        assert len(rows) == 2
        assert rows_written == 2

    def test_observation_date_is_first_of_month(self, engine, session_factory) -> None:
        """BLS period 'M01' for year 2024 → observation_date = '2024-01-01'."""
        fake_client = _fake_client(
            [
                _make_bls_response(
                    "CES0000000001",
                    [("2024", "M03", "156789")],
                )
            ]
        )

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            collect_series(
                series_ids=["CES0000000001"],
                since=date(2024, 3, 1),
                api_key="testkey",
                session_factory=session_factory,
            )

        with Session(engine) as sess:
            row = sess.query(MacroObservations).filter_by(series_id="CES0000000001").one()
        assert row.observation_date == "2024-03-01"

    def test_source_is_bls(self, engine, session_factory) -> None:
        fake_client = _fake_client(
            [
                _make_bls_response("LNS14000000", [("2024", "M01", "3.7")])
            ]
        )

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
            )

        with Session(engine) as sess:
            row = sess.query(MacroObservations).filter_by(series_id="LNS14000000").one()
        assert row.source == "bls"

    def test_first_write_has_revision_number_zero(self, engine, session_factory) -> None:
        fake_client = _fake_client(
            [
                _make_bls_response("LNS14000000", [("2024", "M01", "3.7")])
            ]
        )

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
            )

        with Session(engine) as sess:
            row = sess.query(MacroObservations).filter_by(series_id="LNS14000000").one()
        assert row.revision_number == 0

    def test_frequency_and_units_come_from_series_registry(self, engine, session_factory) -> None:
        fake_client = _fake_client(
            [
                _make_bls_response("LNS14000000", [("2024", "M01", "3.7")])
            ]
        )

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
            )

        with Session(engine) as sess:
            row = sess.query(MacroObservations).filter_by(series_id="LNS14000000").one()
        assert row.frequency == "monthly"
        assert row.units == "pct"


# ---------------------------------------------------------------------------
# AC: revision detection
# ---------------------------------------------------------------------------


class TestRevisionDetection:
    def test_revised_value_stored_with_incremented_revision_number(
        self, engine, session_factory
    ) -> None:
        """If the stored value differs from the incoming value, insert a new row with revision_number+1."""
        # First collection: value = 3.7
        fake_client_1 = _fake_client(
            [_make_bls_response("LNS14000000", [("2024", "M01", "3.7")])]
        )
        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client_1):
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
            )

        # Second collection: BLS revised the value to 3.8
        fake_client_2 = _fake_client(
            [_make_bls_response("LNS14000000", [("2024", "M01", "3.8")])]
        )
        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client_2):
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
            )

        with Session(engine) as sess:
            rows = (
                sess.query(MacroObservations)
                .filter_by(series_id="LNS14000000", observation_date="2024-01-01")
                .order_by(MacroObservations.revision_number)
                .all()
            )
        assert len(rows) == 2
        assert rows[0].revision_number == 0
        assert rows[0].value == pytest.approx(3.7)
        assert rows[1].revision_number == 1
        assert rows[1].value == pytest.approx(3.8)

    def test_unchanged_value_does_not_add_revision(self, engine, session_factory) -> None:
        """If the value is unchanged, no new revision row is written."""
        for _ in range(2):
            fake_client = _fake_client(
                [_make_bls_response("LNS14000000", [("2024", "M01", "3.7")])]
            )
            with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
                collect_series(
                    series_ids=["LNS14000000"],
                    since=date(2024, 1, 1),
                    api_key="testkey",
                    session_factory=session_factory,
                )

        with Session(engine) as sess:
            rows = (
                sess.query(MacroObservations)
                .filter_by(series_id="LNS14000000", observation_date="2024-01-01")
                .all()
            )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# AC: no duplicate rows on re-run with same window
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_rerun_same_window_produces_no_duplicates(self, engine, session_factory) -> None:
        observations = [("2024", "M01", "3.7"), ("2024", "M02", "3.9")]

        for _ in range(3):
            fake_client = _fake_client(
                [_make_bls_response("LNS14000000", observations)]
            )
            with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
                collect_series(
                    series_ids=["LNS14000000"],
                    since=date(2024, 1, 1),
                    api_key="testkey",
                    session_factory=session_factory,
                )

        with Session(engine) as sess:
            rows = sess.query(MacroObservations).filter_by(series_id="LNS14000000").all()
        # Same values repeated → only 2 rows (one per observation date)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# AC: on failure, collection_runs records 'failed'; no data rows written
# ---------------------------------------------------------------------------


class TestFailureBehavior:
    def test_http_failure_records_failed_run_no_data(self, engine, session_factory) -> None:
        fake_client = MagicMock()
        request = httpx.Request("POST", "https://api.bls.gov/publicAPI/v2/timeseries/data/")
        response = httpx.Response(500, request=request)
        fake_client.post_timeseries.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error", request=request, response=response
        )

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            with pytest.raises(httpx.HTTPStatusError):
                collect_series(
                    series_ids=["LNS14000000"],
                    since=date(2024, 1, 1),
                    api_key="testkey",
                    session_factory=session_factory,
                )

        with Session(engine) as sess:
            runs = sess.query(CollectionRuns).all()
            obs = sess.query(MacroObservations).all()

        assert len(obs) == 0
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert "HTTPStatusError" in (runs[0].error_summary or "")


# ---------------------------------------------------------------------------
# AC: bootstrap_series pulls 24 months of history
# ---------------------------------------------------------------------------


class TestBootstrapSeries:
    def test_bootstrap_requests_24_months_of_history(self, engine, session_factory) -> None:
        """bootstrap_series should cover the last 24 months."""
        fake_client = MagicMock()
        fake_client.post_timeseries.return_value = []

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            bootstrap_series(
                api_key="testkey",
                session_factory=session_factory,
                _now=datetime(2026, 4, 1, tzinfo=timezone.utc),
            )

        # bootstrap calls post_timeseries; check the year range spans 24 months
        assert fake_client.post_timeseries.called
        call_kwargs = fake_client.post_timeseries.call_args_list[0][1]
        start_year = int(call_kwargs["start_year"])
        end_year = int(call_kwargs["end_year"])
        # 24 months back from 2026-04 → start = 2024
        assert start_year == 2024
        assert end_year == 2026

    def test_bootstrap_writes_rows_for_all_configured_series(
        self, engine, session_factory
    ) -> None:
        """bootstrap_series fetches every series in the SERIES registry."""
        from alphamind.data_sources.bls.series import SERIES

        fake_client = MagicMock()
        # Return one observation per series for the batch
        def fake_post(series_ids, *, start_year, end_year):
            return [
                {
                    "seriesID": sid,
                    "data": [{"year": "2024", "period": "M01", "value": "1.0", "footnotes": [{}]}],
                }
                for sid in series_ids
            ]

        fake_client.post_timeseries.side_effect = fake_post

        with patch("alphamind.data_sources.bls.macro.BLSClient", return_value=fake_client):
            bootstrap_series(
                api_key="testkey",
                session_factory=session_factory,
                _now=datetime(2026, 4, 1, tzinfo=timezone.utc),
            )

        with Session(engine) as sess:
            series_ids_written = {
                r.series_id for r in sess.query(MacroObservations).all()
            }

        assert series_ids_written == set(SERIES.keys())
