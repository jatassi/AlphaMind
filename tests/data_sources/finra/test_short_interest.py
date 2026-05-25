"""Tests for finra/short_interest.py — all FINRA SDK calls are routed
through FakeFinraAPI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import AssetUniverse, Base, ShortInterestSnapshot
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.finra import FakeFinraAPI
from tests.data_sources._fakes.run_repo import FakeRunRepo

# FINRA settlement date used throughout tests (real FINRA schedule: ~15th of month)
_SETTLEMENT_DATE = "2026-01-15"
_SETTLEMENT_YYYYMMDD = "20260115"


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = make_session_factory(engine)
    with sf() as sess:
        sess.add(
            AssetUniverse(
                asset_id="u1",
                ticker=Symbol("AAPL"),
                full_name="Apple Inc.",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-01-01T00:00:00+00:00",
            )
        )
        sess.commit()
    return sf


def _csv_body(rows: list[dict[str, Any]]) -> str:
    """Build a minimal FINRA short interest CSV body."""
    header = (
        "settlementDate,symbol,currentShortPositionQuantity,"
        "previousShortPositionQuantity,averageDailyShareVolumeQuantity,"
        "daysToCoverQuantity,changePercent"
    )
    lines = [header]
    for row in rows:
        lines.append(
            f"{row.get('settlementDate', '')},{row.get('symbol', '')},"
            f"{row.get('currentShortPositionQuantity', 0)},"
            f"{row.get('previousShortPositionQuantity', 0)},"
            f"{row.get('averageDailyShareVolumeQuantity', 0)},"
            f"{row.get('daysToCoverQuantity', 0)},"
            f"{row.get('changePercent', 0.0)}"
        )
    return "\n".join(lines) + "\n"


def _path_for(yyyymmdd: str) -> str:
    return f"/equity/otcmarket/biweekly/shrt{yyyymmdd}.csv"


class TestCollectShortInterest:
    def test_writes_rows_with_source_finra(self, session_factory: sessionmaker[Session]) -> None:
        """New settlement-date file rows are written with source='finra'."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        path = _path_for(_SETTLEMENT_YYYYMMDD)
        body = _csv_body(
            [
                {
                    "settlementDate": _SETTLEMENT_DATE,
                    "symbol": "AAPL",
                    "currentShortPositionQuantity": 100_000,
                    "previousShortPositionQuantity": 90_000,
                    "averageDailyShareVolumeQuantity": 50_000_000,
                    "daysToCoverQuantity": 2,
                    "changePercent": 11.1,
                }
            ]
        )
        client = FakeFinraAPI(responses={path: body})

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )

        with session_factory() as sess:
            row = sess.get(ShortInterestSnapshot, (_SETTLEMENT_DATE, "AAPL"))
            assert row is not None
            assert row.current_short_shares == 100_000
            assert row.source == "finra"

    def test_filters_non_universe_tickers(self, session_factory: sessionmaker[Session]) -> None:
        """Rows for tickers not in asset_universe are not written."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        path = _path_for(_SETTLEMENT_YYYYMMDD)
        body = _csv_body(
            [
                {
                    "settlementDate": _SETTLEMENT_DATE,
                    "symbol": "AAPL",
                    "currentShortPositionQuantity": 100_000,
                    "previousShortPositionQuantity": 90_000,
                    "averageDailyShareVolumeQuantity": 50_000_000,
                    "daysToCoverQuantity": 2,
                    "changePercent": 11.1,
                },
                {
                    "settlementDate": _SETTLEMENT_DATE,
                    "symbol": "UNKNOWN_XYZ",
                    "currentShortPositionQuantity": 5_000,
                    "previousShortPositionQuantity": 4_000,
                    "averageDailyShareVolumeQuantity": 100_000,
                    "daysToCoverQuantity": 1,
                    "changePercent": 25.0,
                },
            ]
        )
        client = FakeFinraAPI(responses={path: body})

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )

        with session_factory() as sess:
            rows = sess.query(ShortInterestSnapshot).all()
            tickers = {r.ticker for r in rows}
            assert "UNKNOWN_XYZ" not in tickers
            assert "AAPL" in tickers

    def test_idempotent_rerun(self, session_factory: sessionmaker[Session]) -> None:
        """Re-running on the same settlement date produces no duplicate rows."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        path = _path_for(_SETTLEMENT_YYYYMMDD)
        body = _csv_body(
            [
                {
                    "settlementDate": _SETTLEMENT_DATE,
                    "symbol": "AAPL",
                    "currentShortPositionQuantity": 100_000,
                    "previousShortPositionQuantity": 90_000,
                    "averageDailyShareVolumeQuantity": 50_000_000,
                    "daysToCoverQuantity": 2,
                    "changePercent": 11.1,
                }
            ]
        )
        client = FakeFinraAPI(responses={path: body})

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )
        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )

        with session_factory() as sess:
            assert sess.query(ShortInterestSnapshot).count() == 1

    def test_404_pre_sla_silent(self, session_factory: sessionmaker[Session]) -> None:
        """A 404 within the 10-day publication lag is silently skipped — no error_summary."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        client = FakeFinraAPI()  # all paths → 404
        repo = FakeRunRepo()
        # Five days after settlement — inside FINRA's ~7-10 day publication lag.
        now_pre_sla = datetime(2026, 1, 20, 15, 0, tzinfo=UTC)

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _now=now_pre_sla,
        )

        with session_factory() as sess:
            assert sess.query(ShortInterestSnapshot).count() == 0
        assert repo.latest()["status"] == "success"
        assert repo.latest()["error_summary"] is None

    def test_404_past_sla_records_error_summary(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """A 404 more than 10 calendar days past settlement records a post-SLA marker."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        client = FakeFinraAPI()  # all paths → 404
        repo = FakeRunRepo()
        # Four months past settlement — well outside the ~7-10 day publication lag.
        now_past_sla = datetime(2026, 5, 25, 15, 0, tzinfo=UTC)

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _now=now_past_sla,
        )

        with session_factory() as sess:
            assert sess.query(ShortInterestSnapshot).count() == 0
        latest = repo.latest()
        assert latest["status"] == "success"
        assert latest["error_summary"] is not None
        assert _SETTLEMENT_DATE in latest["error_summary"]
        assert "past sla" in latest["error_summary"].lower()

    def test_404_on_us_market_holiday_not_flagged_post_sla(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """A 404 on a US-market-holiday settlement date is silently skipped."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        # 2026-01-19 is Martin Luther King Jr. Day — a US market holiday.
        mlk_iso = "2026-01-19"
        client = FakeFinraAPI()  # all paths → 404
        repo = FakeRunRepo()
        now_past_sla = datetime(2026, 5, 25, 15, 0, tzinfo=UTC)

        collect_short_interest(
            settlement_dates=[mlk_iso],
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _now=now_past_sla,
        )

        assert repo.latest()["status"] == "success"
        assert repo.latest()["error_summary"] is None

    def test_on_failure_collection_runs_records_failed(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Unexpected error causes collection_runs to record 'failed'."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        path = _path_for(_SETTLEMENT_YYYYMMDD)
        client = FakeFinraAPI(responses={path: RuntimeError("unexpected")})
        repo = FakeRunRepo()

        with pytest.raises(RuntimeError, match="unexpected"):
            collect_short_interest(
                settlement_dates=[_SETTLEMENT_DATE],
                client=client,
                session_factory=session_factory,
                _repo=repo,
            )

        assert repo.failed()
