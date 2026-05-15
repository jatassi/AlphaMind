"""
Tests for bls/macro.py.

All BLS SDK access is routed through FakeBLSAPI.  DB uses an in-memory
SQLite engine.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alphamind.data_sources.bls.macro import bootstrap_series, collect_series
from alphamind.persistence.models import Base, CollectionRuns, MacroObservations
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.bls import FakeBLSAPI, make_bls_series

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    eng: Engine = make_engine(":memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    sf: sessionmaker[Session] = make_session_factory(engine)
    return sf


def _fake_client(responses: list[list[dict[str, Any]]]) -> FakeBLSAPI:
    return FakeBLSAPI(responses=responses)


# ---------------------------------------------------------------------------
# AC: collect_series writes macro_observations rows
# ---------------------------------------------------------------------------


class TestCollectSeries:
    def test_writes_rows_for_each_observation(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        client = _fake_client(
            [
                [
                    make_bls_series(
                        "LNS14000000",
                        [("2024", "M01", "3.7"), ("2024", "M02", "3.9")],
                    )
                ]
            ]
        )

        rows_written = collect_series(
            series_ids=["LNS14000000"],
            since=date(2024, 1, 1),
            api_key="testkey",
            session_factory=session_factory,
            client=client,
        )

        with Session(engine) as sess:
            rows = sess.query(MacroObservations).filter_by(series_id="LNS14000000").all()
        assert len(rows) == 2
        assert rows_written == 2

    def test_observation_date_is_first_of_month(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        """BLS period 'M01' for year 2024 → observation_date = '2024-01-01'."""
        client = _fake_client([[make_bls_series("CES0000000001", [("2024", "M03", "156789")])]])

        collect_series(
            series_ids=["CES0000000001"],
            since=date(2024, 3, 1),
            api_key="testkey",
            session_factory=session_factory,
            client=client,
        )

        with Session(engine) as sess:
            row = sess.query(MacroObservations).filter_by(series_id="CES0000000001").one()
        assert row.observation_date == "2024-03-01"

    def test_source_is_bls(self, engine: Engine, session_factory: sessionmaker[Session]) -> None:
        client = _fake_client([[make_bls_series("LNS14000000", [("2024", "M01", "3.7")])]])

        collect_series(
            series_ids=["LNS14000000"],
            since=date(2024, 1, 1),
            api_key="testkey",
            session_factory=session_factory,
            client=client,
        )

        with Session(engine) as sess:
            row = sess.query(MacroObservations).filter_by(series_id="LNS14000000").one()
        assert row.source == "bls"

    def test_first_write_has_revision_number_zero(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        client = _fake_client([[make_bls_series("LNS14000000", [("2024", "M01", "3.7")])]])

        collect_series(
            series_ids=["LNS14000000"],
            since=date(2024, 1, 1),
            api_key="testkey",
            session_factory=session_factory,
            client=client,
        )

        with Session(engine) as sess:
            row = sess.query(MacroObservations).filter_by(series_id="LNS14000000").one()
        assert row.revision_number == 0

    def test_frequency_and_units_come_from_series_registry(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        client = _fake_client([[make_bls_series("LNS14000000", [("2024", "M01", "3.7")])]])

        collect_series(
            series_ids=["LNS14000000"],
            since=date(2024, 1, 1),
            api_key="testkey",
            session_factory=session_factory,
            client=client,
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
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        """If stored value differs from incoming value, insert a new row with revision_number+1."""
        # First collection: value = 3.7
        client_1 = _fake_client([[make_bls_series("LNS14000000", [("2024", "M01", "3.7")])]])
        collect_series(
            series_ids=["LNS14000000"],
            since=date(2024, 1, 1),
            api_key="testkey",
            session_factory=session_factory,
            client=client_1,
        )

        # Second collection: BLS revised the value to 3.8
        client_2 = _fake_client([[make_bls_series("LNS14000000", [("2024", "M01", "3.8")])]])
        collect_series(
            series_ids=["LNS14000000"],
            since=date(2024, 1, 1),
            api_key="testkey",
            session_factory=session_factory,
            client=client_2,
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

    def test_unchanged_value_does_not_add_revision(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        """If the value is unchanged, no new revision row is written."""
        for _ in range(2):
            client = _fake_client([[make_bls_series("LNS14000000", [("2024", "M01", "3.7")])]])
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
                client=client,
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
    def test_rerun_same_window_produces_no_duplicates(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        observations = [("2024", "M01", "3.7"), ("2024", "M02", "3.9")]

        for _ in range(3):
            client = _fake_client([[make_bls_series("LNS14000000", observations)]])
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
                client=client,
            )

        with Session(engine) as sess:
            rows = sess.query(MacroObservations).filter_by(series_id="LNS14000000").all()
        # Same values repeated → only 2 rows (one per observation date)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# AC: on failure, collection_runs records 'failed'; no data rows written
# ---------------------------------------------------------------------------


class TestFailureBehavior:
    def test_http_failure_records_failed_run_no_data(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        request = httpx.Request("POST", "https://api.bls.gov/publicAPI/v2/timeseries/data/")
        response = httpx.Response(500, request=request)
        client = FakeBLSAPI(
            error=httpx.HTTPStatusError(
                "500 Internal Server Error", request=request, response=response
            )
        )

        with pytest.raises(httpx.HTTPStatusError):
            collect_series(
                series_ids=["LNS14000000"],
                since=date(2024, 1, 1),
                api_key="testkey",
                session_factory=session_factory,
                client=client,
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
    def test_bootstrap_requests_24_months_of_history(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        """bootstrap_series should cover the last 24 months."""
        client = FakeBLSAPI()  # default: empty responses

        bootstrap_series(
            api_key="testkey",
            session_factory=session_factory,
            client=client,
            _now=datetime(2026, 4, 1, tzinfo=UTC),
        )

        # bootstrap calls post_timeseries; check the year range spans 24 months
        assert len(client.calls) >= 1
        first = client.calls[0]
        start_year = int(first["start_year"])
        end_year = int(first["end_year"])
        assert start_year == 2024
        assert end_year == 2026

    def test_bootstrap_writes_rows_for_all_configured_series(
        self, engine: Engine, session_factory: sessionmaker[Session]
    ) -> None:
        """bootstrap_series fetches every series in the SERIES registry."""
        from alphamind.data_sources.bls.series import SERIES

        def handler(
            series_ids: list[str], *, start_year: str, end_year: str
        ) -> list[dict[str, Any]]:
            return [make_bls_series(sid, [("2024", "M01", "1.0")]) for sid in series_ids]

        client = FakeBLSAPI(handler=handler)

        bootstrap_series(
            api_key="testkey",
            session_factory=session_factory,
            client=client,
            _now=datetime(2026, 4, 1, tzinfo=UTC),
        )

        with Session(engine) as sess:
            series_ids_written = {r.series_id for r in sess.query(MacroObservations).all()}

        assert series_ids_written == set(SERIES.keys())
