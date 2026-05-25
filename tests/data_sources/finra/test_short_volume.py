"""Tests for finra/short_volume.py — all FINRA SDK calls routed through FakeFinraAPI."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import AssetUniverse, Base, ShortVolumeDaily
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.finra import FakeFinraAPI
from tests.data_sources._fakes.run_repo import FakeRunRepo

# Trade date used throughout: 2026-01-23 is a Friday
_TRADE_DATE = date(2026, 1, 23)
_TRADE_DATE_STR = "20260123"
_TRADE_DATE_ISO = "2026-01-23"


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


def _file_body(rows: list[tuple[str, str, int, int, int, str]]) -> str:
    """Build a mock FINRA pipe-delimited file body.

    Each row tuple: (date_yyyymmdd, symbol, short_vol, exempt_vol, total_vol, market)
    """
    header = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"
    lines = [header]
    for date_str, sym, sv, sev, tv, mkt in rows:
        lines.append(f"{date_str}|{sym}|{sv}|{sev}|{tv}|{mkt}")
    return "\n".join(lines) + "\n"


def _path_for(trade_date: date) -> str:
    return f"/equity/regsho/daily/CNMSshvol{trade_date.strftime('%Y%m%d')}.txt"


class TestCollectShortVolume:
    def test_writes_rows_with_cnms_market(self, session_factory: sessionmaker[Session]) -> None:
        """Rows are written with market='cnms' and source='finra'."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        body = _file_body([(_TRADE_DATE_STR, "AAPL", 2_000_000, 10_000, 5_000_000, "CNMS")])
        client = FakeFinraAPI(responses={path: body})

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )

        with session_factory() as sess:
            row = sess.get(ShortVolumeDaily, (_TRADE_DATE_ISO, "AAPL", "cnms"))
            assert row is not None
            assert row.short_volume == 2_000_000
            assert row.market == "cnms"
            assert row.source == "finra"

    def test_filters_non_universe_tickers(self, session_factory: sessionmaker[Session]) -> None:
        """Rows for tickers not in asset_universe are not written."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        body = _file_body(
            [
                (_TRADE_DATE_STR, "AAPL", 2_000_000, 10_000, 5_000_000, "CNMS"),
                (_TRADE_DATE_STR, "UNKNOWN_XYZ", 500_000, 0, 1_000_000, "CNMS"),
            ]
        )
        client = FakeFinraAPI(responses={path: body})

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )

        with session_factory() as sess:
            rows = sess.query(ShortVolumeDaily).all()
            tickers = {r.ticker for r in rows}
            assert "UNKNOWN_XYZ" not in tickers
            assert "AAPL" in tickers

    def test_idempotent_rerun(self, session_factory: sessionmaker[Session]) -> None:
        """Re-running on the same date produces no duplicate rows."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        body = _file_body([(_TRADE_DATE_STR, "AAPL", 2_000_000, 10_000, 5_000_000, "CNMS")])
        client = FakeFinraAPI(responses={path: body})

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )
        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=FakeRunRepo(),
        )

        with session_factory() as sess:
            assert sess.query(ShortVolumeDaily).count() == 1

    def test_404_pre_sla_silent(self, session_factory: sessionmaker[Session]) -> None:
        """A 404 before FINRA's 6pm-ET SLA is silently skipped — no rows, no error_summary."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        client = FakeFinraAPI()  # all paths → 404
        repo = FakeRunRepo()
        # 9am ET on the trade date — file not due until 6pm ET.
        now_pre_sla = datetime(2026, 1, 23, 14, 0, tzinfo=UTC)

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _now=now_pre_sla,
        )

        with session_factory() as sess:
            assert sess.query(ShortVolumeDaily).count() == 0
        assert repo.latest()["status"] == "success"
        assert repo.latest()["error_summary"] is None

    def test_404_past_sla_records_error_summary(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """A 404 after the publication SLA records a post-SLA marker in error_summary."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        client = FakeFinraAPI()  # all paths → 404
        repo = FakeRunRepo()
        # Four months past the trade date — well past the 6pm-ET-same-day SLA.
        now_past_sla = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _now=now_past_sla,
        )

        with session_factory() as sess:
            assert sess.query(ShortVolumeDaily).count() == 0
        latest = repo.latest()
        assert latest["status"] == "success"
        assert latest["error_summary"] is not None
        assert _TRADE_DATE_ISO in latest["error_summary"]
        assert "past sla" in latest["error_summary"].lower()

    def test_404_on_us_market_holiday_not_flagged_post_sla(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """A 404 on a US market holiday is silently skipped — FINRA does not publish."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        # 2026-01-19 is Martin Luther King Jr. Day — a US market holiday and a Monday.
        mlk_day = date(2026, 1, 19)
        client = FakeFinraAPI()  # all paths → 404
        repo = FakeRunRepo()
        now_past_sla = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)

        collect_short_volume(
            since=mlk_day,
            until=mlk_day,
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
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        client = FakeFinraAPI(responses={path: RuntimeError("unexpected")})
        repo = FakeRunRepo()

        with pytest.raises(RuntimeError, match="unexpected"):
            collect_short_volume(
                since=_TRADE_DATE,
                until=_TRADE_DATE,
                client=client,
                session_factory=session_factory,
                _repo=repo,
            )

        assert repo.failed()
