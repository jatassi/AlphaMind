"""Tests for finra/short_volume.py — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.orm import Session

from alphamind.persistence.models import AssetUniverse, Base, ShortVolumeDaily
from alphamind.persistence.session import make_engine, make_session_factory

# Trade date used throughout: 2026-01-23 is a Friday
_TRADE_DATE = date(2026, 1, 23)
_TRADE_DATE_STR = "20260123"
_TRADE_DATE_ISO = "2026-01-23"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> type[Session]:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf: type[Session] = make_session_factory(engine)
    with sf() as sess:
        sess.add(
            AssetUniverse(
                asset_id="u1",
                ticker="AAPL",
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


def _make_client(responses: dict[str, str | Exception]) -> MagicMock:
    """Return a mock FinraClient whose get() dispatches on path."""

    def fake_get(path: str) -> str:
        val = responses.get(path)
        if val is None:
            req = httpx.Request("GET", f"https://cdn.finra.org{path}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)
        if isinstance(val, Exception):
            raise val
        return val

    client = MagicMock()
    client.get.side_effect = fake_get
    return client


def _path_for(trade_date: date) -> str:
    return f"/equity/regsho/daily/CNMSshvol{trade_date.strftime('%Y%m%d')}.txt"


# ---------------------------------------------------------------------------
# collect_short_volume — happy path
# ---------------------------------------------------------------------------


class TestCollectShortVolume:
    def test_writes_rows_with_cnms_market(self, session_factory: type[Session]) -> None:
        """Rows are written with market='cnms' and source='finra'."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        body = _file_body([(_TRADE_DATE_STR, "AAPL", 2_000_000, 10_000, 5_000_000, "CNMS")])
        client = _make_client({path: body})

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )

        with session_factory() as sess:
            row = sess.get(ShortVolumeDaily, (_TRADE_DATE_ISO, "AAPL", "cnms"))
            assert row is not None
            assert row.short_volume == 2_000_000
            assert row.market == "cnms"
            assert row.source == "finra"

    def test_filters_non_universe_tickers(self, session_factory: type[Session]) -> None:
        """Rows for tickers not in asset_universe are not written."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        body = _file_body(
            [
                (_TRADE_DATE_STR, "AAPL", 2_000_000, 10_000, 5_000_000, "CNMS"),
                (_TRADE_DATE_STR, "UNKNOWN_XYZ", 500_000, 0, 1_000_000, "CNMS"),
            ]
        )
        client = _make_client({path: body})

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )

        with session_factory() as sess:
            rows = sess.query(ShortVolumeDaily).all()
            tickers = {r.ticker for r in rows}
            assert "UNKNOWN_XYZ" not in tickers
            assert "AAPL" in tickers

    def test_idempotent_rerun(self, session_factory: type[Session]) -> None:
        """Re-running on the same date produces no duplicate rows."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        body = _file_body([(_TRADE_DATE_STR, "AAPL", 2_000_000, 10_000, 5_000_000, "CNMS")])
        client = _make_client({path: body})

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )
        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )

        with session_factory() as sess:
            assert sess.query(ShortVolumeDaily).count() == 1

    def test_404_treated_as_not_yet_published(self, session_factory: type[Session]) -> None:
        """A 404 on a future-dated file is silently skipped — no rows, no error."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        client = _make_client({})  # all paths → 404

        collect_short_volume(
            since=_TRADE_DATE,
            until=_TRADE_DATE,
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )

        with session_factory() as sess:
            assert sess.query(ShortVolumeDaily).count() == 0

    def test_on_failure_collection_runs_records_failed(
        self, session_factory: type[Session]
    ) -> None:
        """Unexpected error causes collection_runs to record 'failed'."""
        from alphamind.data_sources.finra.short_volume import collect_short_volume

        path = _path_for(_TRADE_DATE)
        client = _make_client({path: RuntimeError("unexpected")})
        mock_repo = MagicMock()

        with pytest.raises(RuntimeError, match="unexpected"):
            collect_short_volume(
                since=_TRADE_DATE,
                until=_TRADE_DATE,
                client=client,
                session_factory=session_factory,
                _repo=mock_repo,
            )

        mock_repo.update_failed.assert_called_once()
