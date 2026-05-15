"""
Tests for the Finnhub estimate-revisions collector — story 05m.

All Finnhub SDK calls are mocked.  Tests use an in-memory SQLite database
seeded with minimal rows so FK constraints are satisfied.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import Base, EarningsEstimateRevisions
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    sf: sessionmaker[Session] = make_session_factory(engine)
    return sf


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as sess:
        yield sess


@pytest.fixture()
def seeded_tickers(session: Session) -> None:
    """Insert minimal AssetUniverse rows for tickers used in tests."""
    from alphamind.persistence.models import AssetUniverse

    for ticker in ("AAPL", "MSFT"):
        session.add(
            AssetUniverse(
                asset_id=f"asset-{ticker.lower()}",
                ticker=ticker,
                full_name=f"{ticker} Inc.",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
    session.commit()


@pytest.fixture()
def fake_repo() -> Any:
    """In-memory collection_runs repo for test isolation."""

    class _Repo:
        def __init__(self) -> None:
            self.rows: dict[str, dict[str, Any]] = {}

        def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
            self.rows[run_id] = {
                "status": "running",
                "collector": collector,
                "rows_written": None,
                "error_summary": None,
            }

        def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
            self.rows[run_id].update(status="success", rows_written=rows_written)

        def update_failed(self, run_id: str, error_summary: str) -> None:
            self.rows[run_id].update(status="failed", error_summary=error_summary)

    return _Repo()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_eps_response(
    ticker: str = "AAPL",
    periods: list[tuple[str, float, int | None]] | None = None,
) -> dict[str, Any]:
    """Build a fake finnhub company_eps_estimates response."""
    if periods is None:
        periods = [("2026-06-30", 1.5, 10), ("2026-09-30", 1.8, 10)]
    return {
        "symbol": ticker,
        "data": [
            {"period": p, "epsAvg": avg, "numberAnalysts": n_analysts}
            for p, avg, n_analysts in periods
        ],
        "freq": "quarterly",
    }


def _make_revenue_response(
    ticker: str = "AAPL",
    periods: list[tuple[str, float, int | None]] | None = None,
) -> dict[str, Any]:
    """Build a fake finnhub company_revenue_estimates response."""
    if periods is None:
        periods = [("2026-06-30", 95_000_000_000.0, 10), ("2026-09-30", 98_000_000_000.0, 10)]
    return {
        "symbol": ticker,
        "data": [
            {"period": p, "revenueAvg": avg, "numberAnalysts": n_analysts}
            for p, avg, n_analysts in periods
        ],
        "freq": "quarterly",
    }


def _run_collect(
    engine: Engine,
    session_factory: sessionmaker[Session],
    fake_repo: Any,
    ticker_scope: list[str] | None,
    eps_response_map: dict[str, dict[str, Any]],
    revenue_response_map: dict[str, dict[str, Any]],
) -> None:
    """Helper: run collect_estimate_revisions with mocked SDK."""
    with patch("finnhub.Client", autospec=True) as mock_client:
        mock_sdk = mock_client.return_value

        def _eps_side(symbol: str, **_kwargs: Any) -> dict[str, Any]:
            return eps_response_map.get(symbol, {"symbol": symbol, "data": [], "freq": "quarterly"})

        def _rev_side(symbol: str, **_kwargs: Any) -> dict[str, Any]:
            return revenue_response_map.get(
                symbol, {"symbol": symbol, "data": [], "freq": "quarterly"}
            )

        mock_sdk.company_eps_estimates.side_effect = _eps_side
        mock_sdk.company_revenue_estimates.side_effect = _rev_side

        from alphamind.data_sources.finnhub.estimate_revisions import collect_estimate_revisions

        collect_estimate_revisions(
            ticker_scope=ticker_scope,
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
        )


# ---------------------------------------------------------------------------
# Slice 1 — model: EarningsEstimateRevisions table exists and creates cleanly
# ---------------------------------------------------------------------------


class TestEarningsEstimateRevisionsModel:
    def test_table_creation_succeeds(self, engine: Engine, session: Session) -> None:
        """EarningsEstimateRevisions table can be created and queried."""
        rows = session.query(EarningsEstimateRevisions).all()
        assert rows == []

    def test_model_has_required_columns(self) -> None:
        """EarningsEstimateRevisions has the columns the spec demands."""
        cols = {c.name for c in EarningsEstimateRevisions.__table__.columns}
        required = {
            "revised_at",
            "ticker",
            "fiscal_year",
            "fiscal_period",
            "metric",
            "consensus_value",
            "prior_consensus_value",
            "num_analysts",
            "source",
            "ingested_at",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"


# ---------------------------------------------------------------------------
# Slice 2 — no new client.py: estimate_revisions reuses finnhub/client.py
# ---------------------------------------------------------------------------


class TestNoNewClientFile:
    def test_no_separate_client_file_created(self) -> None:
        """There must NOT be a new client.py for estimate_revisions — no separate client module."""
        import sys

        assert "alphamind.data_sources.finnhub.estimate_revisions_client" not in sys.modules

        # No new FinnhubClient class defined in estimate_revisions itself
        from alphamind.data_sources.finnhub import client, estimate_revisions

        assert not hasattr(estimate_revisions, "FinnhubClient") or (
            estimate_revisions.FinnhubClient is client.FinnhubClient
        ), "estimate_revisions must not define its own FinnhubClient"


# ---------------------------------------------------------------------------
# Slice 3 — first observation: row inserted with prior_consensus_value=NULL
# ---------------------------------------------------------------------------


class TestFirstObservation:
    def test_seed_inserts_eps_row_with_null_prior(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """First EPS observation inserts a row with prior_consensus_value=NULL."""
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [("2026-06-30", 1.5, 10)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        rows = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("AAPL"), metric="eps")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].consensus_value == pytest.approx(1.5)
        assert rows[0].prior_consensus_value is None

    def test_seed_inserts_revenue_row_with_null_prior(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """First revenue observation inserts a row with prior_consensus_value=NULL."""
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [])},
            revenue_response_map={
                "AAPL": _make_revenue_response("AAPL", [("2026-06-30", 95e9, 10)])
            },
        )

        rows = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("AAPL"), metric="revenue")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].consensus_value == pytest.approx(95e9)
        assert rows[0].prior_consensus_value is None

    def test_num_analysts_populated_when_present(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """num_analysts is populated from numberAnalysts when present."""
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [("2026-06-30", 1.5, 12)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        row = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("AAPL"), metric="eps")
            .first()
        )
        assert row is not None
        assert row.num_analysts == 12

    def test_num_analysts_null_when_omitted(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """num_analysts is NULL when numberAnalysts is absent in response."""
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [("2026-06-30", 1.5, None)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        row = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("AAPL"), metric="eps")
            .first()
        )
        assert row is not None
        assert row.num_analysts is None


# ---------------------------------------------------------------------------
# Slice 4 — changed consensus: new row written with prior populated
# ---------------------------------------------------------------------------


class TestChangedConsensus:
    def test_changed_consensus_inserts_new_row_with_prior_value(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """When consensus changes, a new row is written with prior_consensus_value set."""
        period = "2026-06-30"

        # First run: seed
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [(period, 1.5, 10)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        # Second run: different consensus
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [(period, 1.7, 10)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        rows = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("AAPL"), metric="eps")
            .order_by(EarningsEstimateRevisions.revised_at)
            .all()
        )
        assert len(rows) == 2
        assert rows[0].consensus_value == pytest.approx(1.5)
        assert rows[0].prior_consensus_value is None
        assert rows[1].consensus_value == pytest.approx(1.7)
        assert rows[1].prior_consensus_value == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Slice 5 — unchanged consensus: no new row
# ---------------------------------------------------------------------------


class TestUnchangedConsensus:
    def test_unchanged_consensus_produces_no_new_row(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """Re-running with the same consensus value does not write a new row."""
        period = "2026-06-30"

        # First run: seed
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [(period, 1.5, 10)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        # Second run: same consensus
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [(period, 1.5, 10)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        rows = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("AAPL"), metric="eps")
            .all()
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Slice 6 — universe filter: only active tickers processed
# ---------------------------------------------------------------------------


class TestUniverseFilter:
    def test_inactive_ticker_not_processed(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: Any,
    ) -> None:
        """Tickers with is_active=0 are excluded from collection."""
        from alphamind.persistence.models import AssetUniverse

        session.add(
            AssetUniverse(
                asset_id="asset-inactive",
                ticker=Symbol("INACT"),
                full_name="Inactive Corp",
                asset_class="equity",
                asset_role="universe",
                exchange="NYSE",
                is_active=0,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
        session.commit()

        # When ticker_scope is None, only active tickers should be processed
        with patch("finnhub.Client", autospec=True) as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.company_eps_estimates.return_value = {
                "symbol": "INACT",
                "data": [{"period": "2026-06-30", "epsAvg": 1.0, "numberAnalysts": 5}],
                "freq": "quarterly",
            }
            mock_sdk.company_revenue_estimates.return_value = {
                "symbol": "INACT",
                "data": [],
                "freq": "quarterly",
            }

            from alphamind.data_sources.finnhub.estimate_revisions import collect_estimate_revisions

            collect_estimate_revisions(
                ticker_scope=None,  # uses asset_universe filter
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        rows = session.query(EarningsEstimateRevisions).filter_by(ticker=Symbol("INACT")).all()
        assert rows == [], "Inactive ticker should not produce estimate_revisions rows"

    def test_active_ticker_is_processed(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """With ticker_scope=None, active universe tickers are processed."""
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=None,  # default: uses asset_universe
            eps_response_map={
                "AAPL": _make_eps_response("AAPL", [("2026-06-30", 1.5, 10)]),
                "MSFT": _make_eps_response("MSFT", [("2026-06-30", 2.0, 8)]),
            },
            revenue_response_map={},
        )

        rows = session.query(EarningsEstimateRevisions).all()
        tickers = {r.ticker for r in rows}
        assert "AAPL" in tickers
        assert "MSFT" in tickers


# ---------------------------------------------------------------------------
# Slice 7 — fiscal period mapping: uses earnings_event_details when available
# ---------------------------------------------------------------------------


class TestFiscalPeriodMapping:
    def _seed_earnings_event_details(
        self,
        session: Session,
        ticker: str,
        fiscal_year: int,
        fiscal_period: str,
        period_end_date: str,
    ) -> None:
        """Insert event_calendar + earnings_event_details to give the mapping precedent."""
        from alphamind.persistence.models import EarningsEventDetails, EventCalendar

        event_id = f"test-event-{ticker}-{fiscal_year}-{fiscal_period}"
        session.add(
            EventCalendar(
                event_id=event_id,
                event_type="earnings",
                ticker=ticker,
                scheduled_at=f"{period_end_date}T00:00:00+00:00",
                description=None,
                status="confirmed",
                source="finnhub",
                ingested_at="2026-04-01T00:00:00+00:00",
                last_updated="2026-04-01T00:00:00+00:00",
            )
        )
        session.flush()  # ensure EventCalendar is visible for the FK on EarningsEventDetails
        session.add(
            EarningsEventDetails(
                event_id=event_id,
                ticker=ticker,
                fiscal_period=fiscal_period,
                fiscal_year=fiscal_year,
                expected_call_time=None,
                eps_consensus=None,
                eps_actual=None,
                revenue_consensus_usd=None,
                revenue_actual_usd=None,
                source="finnhub",
            )
        )
        session.commit()

    def test_fiscal_period_mapping_uses_event_details_when_available(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """When earnings_event_details has a row for (ticker, fiscal_year, fiscal_period),
        that mapping is used for the period date."""
        # Seed AAPL's non-standard fiscal calendar: "2026-06-30" → Q3/2026
        self._seed_earnings_event_details(session, "AAPL", 2026, "Q3", "2026-06-30")

        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["AAPL"],
            eps_response_map={"AAPL": _make_eps_response("AAPL", [("2026-06-30", 1.5, 10)])},
            revenue_response_map={"AAPL": _make_revenue_response("AAPL", [])},
        )

        row = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("AAPL"), metric="eps")
            .first()
        )
        assert row is not None
        assert row.fiscal_period == "Q3"
        assert row.fiscal_year == 2026

    def test_fiscal_period_fallback_to_calendar_quarter(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """When no earnings_event_details row exists, calendar-quarter mapping is used."""
        # No seed — MSFT has no fiscal calendar entry
        _run_collect(
            engine,
            session_factory,
            fake_repo,
            ticker_scope=["MSFT"],
            eps_response_map={"MSFT": _make_eps_response("MSFT", [("2026-06-30", 2.0, 8)])},
            revenue_response_map={"MSFT": _make_revenue_response("MSFT", [])},
        )

        row = (
            session.query(EarningsEstimateRevisions)
            .filter_by(ticker=Symbol("MSFT"), metric="eps")
            .first()
        )
        assert row is not None
        # 2026-06-30 is end of Q2 by calendar
        assert row.fiscal_period == "Q2"
        assert row.fiscal_year == 2026


# ---------------------------------------------------------------------------
# Slice 8 — bootstrap_estimate_revisions: seeds with NULL prior; idempotent
# ---------------------------------------------------------------------------


class TestBootstrapEstimateRevisions:
    def test_bootstrap_seeds_with_null_prior(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """bootstrap_estimate_revisions() inserts rows with prior_consensus_value=NULL."""
        with patch("finnhub.Client", autospec=True) as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.company_eps_estimates.return_value = _make_eps_response(
                "AAPL", [("2026-06-30", 1.5, 10)]
            )
            mock_sdk.company_revenue_estimates.return_value = _make_revenue_response("AAPL", [])

            from alphamind.data_sources.finnhub.estimate_revisions import (
                bootstrap_estimate_revisions,
            )

            bootstrap_estimate_revisions(
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        rows = session.query(EarningsEstimateRevisions).all()
        assert len(rows) > 0
        for row in rows:
            assert row.prior_consensus_value is None, (
                f"Bootstrap row {row.ticker}/{row.metric} has non-NULL prior"
            )

    def test_bootstrap_is_idempotent(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: Any,
    ) -> None:
        """Running bootstrap_estimate_revisions() twice does not duplicate rows."""

        def _run_bootstrap() -> None:
            with patch("finnhub.Client", autospec=True) as mock_client:
                mock_sdk = mock_client.return_value
                mock_sdk.company_eps_estimates.return_value = _make_eps_response(
                    "AAPL", [("2026-06-30", 1.5, 10)]
                )
                mock_sdk.company_revenue_estimates.return_value = _make_revenue_response("AAPL", [])

                from alphamind.data_sources.finnhub.estimate_revisions import (
                    bootstrap_estimate_revisions,
                )

                bootstrap_estimate_revisions(
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        _run_bootstrap()
        count_after_first = session.query(EarningsEstimateRevisions).count()

        _run_bootstrap()
        count_after_second = session.query(EarningsEstimateRevisions).count()

        assert count_after_first == count_after_second, (
            f"Bootstrap not idempotent: {count_after_first} rows → {count_after_second}"
        )


# ---------------------------------------------------------------------------
# Slice 9 — failure: collection_runs records failed; no data rows
# ---------------------------------------------------------------------------


class TestFailureRecording:
    def test_sdk_failure_records_failed_run(
        self, engine: Engine, session_factory: sessionmaker[Session], fake_repo: Any
    ) -> None:
        """When the SDK raises, collection_runs records 'failed' and no rows are written."""
        with patch("finnhub.Client", autospec=True) as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.company_eps_estimates.side_effect = RuntimeError("API down")
            mock_sdk.company_revenue_estimates.side_effect = RuntimeError("API down")

            from alphamind.data_sources.finnhub.estimate_revisions import collect_estimate_revisions

            with pytest.raises(RuntimeError):
                collect_estimate_revisions(
                    ticker_scope=["AAPL"],
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        run = next(iter(fake_repo.rows.values()))
        assert run["status"] == "failed"

    def test_sdk_failure_writes_no_data_rows(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: Any,
    ) -> None:
        """On failure, no EarningsEstimateRevisions rows are written."""
        with patch("finnhub.Client", autospec=True) as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.company_eps_estimates.side_effect = RuntimeError("API down")

            from alphamind.data_sources.finnhub.estimate_revisions import collect_estimate_revisions

            with pytest.raises(RuntimeError):
                collect_estimate_revisions(
                    ticker_scope=["AAPL"],
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        rows = session.query(EarningsEstimateRevisions).all()
        assert rows == []


# ---------------------------------------------------------------------------
# Slice 10 — scheduler: finnhub.estimate_revisions in COLLECTORS
# ---------------------------------------------------------------------------


class TestSchedulerRegistration:
    def test_finnhub_estimate_revisions_in_collectors(self) -> None:
        """COLLECTORS registry contains 'finnhub.estimate_revisions'."""
        from alphamind.collector.scheduler import COLLECTORS

        assert "finnhub.estimate_revisions" in COLLECTORS, (
            "finnhub.estimate_revisions must be registered in COLLECTORS"
        )

    def test_finnhub_estimate_revisions_callable(self) -> None:
        """The registered callable is callable."""
        from alphamind.collector.scheduler import COLLECTORS

        fn = COLLECTORS["finnhub.estimate_revisions"]
        assert callable(fn)


# ---------------------------------------------------------------------------
# Slice 11 — bootstrap.run_all: invokes bootstrap_estimate_revisions
# ---------------------------------------------------------------------------


class TestBootstrapRunAll:
    def test_run_all_invokes_bootstrap_estimate_revisions(self) -> None:
        """run_all() calls bootstrap_estimate_revisions after the calendar bootstraps."""
        calls: list[str] = []

        from collections.abc import Callable

        def _make_tracker(name: str) -> Callable[..., None]:
            def _fn(*args: object, **kwargs: object) -> None:
                calls.append(name)

            return _fn

        from contextlib import ExitStack

        patches = {
            "alphamind.collector.bootstrap._seed_asset_universe": _make_tracker("seed"),
            "alphamind.collector.bootstrap.collect_reference": _make_tracker("reference"),
            "alphamind.collector.bootstrap.bootstrap_corporate_actions": _make_tracker("corp"),
            "alphamind.collector.bootstrap.bootstrap_universe_bars": _make_tracker("equity"),
            "alphamind.collector.bootstrap._fred_bootstrap_daily": _make_tracker("fred_daily"),
            "alphamind.collector.bootstrap._fred_bootstrap_monthly": _make_tracker("fred_monthly"),
            "alphamind.collector.bootstrap.eia_bootstrap_series": _make_tracker("eia"),
            "alphamind.collector.bootstrap.bootstrap_auctions": _make_tracker("treasury"),
            "alphamind.collector.bootstrap.bls_bootstrap_series": _make_tracker("bls"),
            "alphamind.collector.bootstrap.bootstrap_earnings_calendar": _make_tracker(
                "earnings_cal"
            ),
            "alphamind.collector.bootstrap.bootstrap_economic_calendar": _make_tracker(
                "economic_cal"
            ),
            "alphamind.collector.bootstrap.collect_ipo_calendar": _make_tracker("ipo_cal"),
            "alphamind.collector.bootstrap.collect_fda_calendar": _make_tracker("fda_cal"),
            "alphamind.collector.bootstrap.bootstrap_estimate_revisions": _make_tracker(
                "est_revisions"
            ),
        }

        with ExitStack() as stack:
            for target, fn in patches.items():
                stack.enter_context(patch(target, side_effect=fn))

            from alphamind.collector.bootstrap import run_all

            run_all(only_vendor="finnhub")

        assert "est_revisions" in calls, f"bootstrap_estimate_revisions not called; calls={calls}"

    def test_bootstrap_estimate_revisions_called_after_calendar(self) -> None:
        """bootstrap_estimate_revisions is called after the calendar bootstraps in step 6."""
        calls: list[str] = []

        from collections.abc import Callable

        def _make_tracker(name: str) -> Callable[..., None]:
            def _fn(*args: object, **kwargs: object) -> None:
                calls.append(name)

            return _fn

        from contextlib import ExitStack

        patches = {
            "alphamind.collector.bootstrap._seed_asset_universe": _make_tracker("seed"),
            "alphamind.collector.bootstrap.collect_reference": _make_tracker("reference"),
            "alphamind.collector.bootstrap.bootstrap_corporate_actions": _make_tracker("corp"),
            "alphamind.collector.bootstrap.bootstrap_universe_bars": _make_tracker("equity"),
            "alphamind.collector.bootstrap._fred_bootstrap_daily": _make_tracker("fred_daily"),
            "alphamind.collector.bootstrap._fred_bootstrap_monthly": _make_tracker("fred_monthly"),
            "alphamind.collector.bootstrap.eia_bootstrap_series": _make_tracker("eia"),
            "alphamind.collector.bootstrap.bootstrap_auctions": _make_tracker("treasury"),
            "alphamind.collector.bootstrap.bls_bootstrap_series": _make_tracker("bls"),
            "alphamind.collector.bootstrap.bootstrap_earnings_calendar": _make_tracker(
                "earnings_cal"
            ),
            "alphamind.collector.bootstrap.bootstrap_economic_calendar": _make_tracker(
                "economic_cal"
            ),
            "alphamind.collector.bootstrap.collect_ipo_calendar": _make_tracker("ipo_cal"),
            "alphamind.collector.bootstrap.collect_fda_calendar": _make_tracker("fda_cal"),
            "alphamind.collector.bootstrap.bootstrap_estimate_revisions": _make_tracker(
                "est_revisions"
            ),
        }

        with ExitStack() as stack:
            for target, fn in patches.items():
                stack.enter_context(patch(target, side_effect=fn))

            from alphamind.collector.bootstrap import run_all

            run_all(only_vendor="finnhub")

        # est_revisions must appear after the calendar bootstraps
        cal_idx = max(
            calls.index("earnings_cal"),
            calls.index("economic_cal"),
        )
        est_idx = calls.index("est_revisions")
        assert est_idx > cal_idx, (
            f"est_revisions (pos {est_idx}) must follow calendar (pos {cal_idx})"
        )


# ---------------------------------------------------------------------------
# Slice 12 — SDK API drift guard (regression for ALP-405)
# ---------------------------------------------------------------------------


class TestSdkApiDrift:
    """The fetch helpers must call methods that actually exist on finnhub.Client.

    Uses ``spec=finnhub.Client`` so accessing a non-existent attribute raises
    AttributeError at test time. Without this, plain MagicMock auto-vivifies
    any attribute and a typoed/renamed SDK method name slips into production
    (root cause of ALP-405).
    """

    def test_fetch_eps_estimates_calls_real_sdk_method(self) -> None:
        from unittest.mock import MagicMock

        import finnhub

        from alphamind.data_sources.finnhub.estimate_revisions import _fetch_eps_estimates

        sdk = MagicMock(spec=finnhub.Client)
        sdk.company_eps_estimates.return_value = {
            "data": [{"period": "2026-06-30", "epsAvg": 1.5, "numberAnalysts": 10}],
            "freq": "quarterly",
            "symbol": "AAPL",
        }

        result = _fetch_eps_estimates(sdk, "AAPL")

        sdk.company_eps_estimates.assert_called_once_with("AAPL")
        assert result == [{"period": "2026-06-30", "epsAvg": 1.5, "numberAnalysts": 10}]

    def test_fetch_revenue_estimates_calls_real_sdk_method(self) -> None:
        from unittest.mock import MagicMock

        import finnhub

        from alphamind.data_sources.finnhub.estimate_revisions import _fetch_revenue_estimates

        sdk = MagicMock(spec=finnhub.Client)
        sdk.company_revenue_estimates.return_value = {
            "data": [{"period": "2026-06-30", "revenueAvg": 95e9, "numberAnalysts": 10}],
            "freq": "quarterly",
            "symbol": "AAPL",
        }

        result = _fetch_revenue_estimates(sdk, "AAPL")

        sdk.company_revenue_estimates.assert_called_once_with("AAPL")
        assert result == [{"period": "2026-06-30", "revenueAvg": 95e9, "numberAnalysts": 10}]
