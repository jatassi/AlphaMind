"""Tests for finra/short_interest.py — all HTTP calls are mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import AssetUniverse, Base, ShortInterestSnapshot
from alphamind.persistence.session import make_engine, make_session_factory

# FINRA settlement date used throughout tests (real FINRA schedule: ~15th of month)
_SETTLEMENT_DATE = "2026-01-15"
_SETTLEMENT_YYYYMMDD = "20260115"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _path_for(yyyymmdd: str) -> str:
    return f"/equity/otcmarket/biweekly/shrt{yyyymmdd}.csv"


# ---------------------------------------------------------------------------
# collect_short_interest — happy path
# ---------------------------------------------------------------------------


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
        client = _make_client({path: body})

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
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
        client = _make_client({path: body})

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
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
        client = _make_client({path: body})

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )
        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )

        with session_factory() as sess:
            assert sess.query(ShortInterestSnapshot).count() == 1

    def test_404_treated_as_not_yet_published(self, session_factory: sessionmaker[Session]) -> None:
        """A 404 on a settlement-date file is silently skipped — no rows, no error."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        client = _make_client({})  # all paths → 404

        collect_short_interest(
            settlement_dates=[_SETTLEMENT_DATE],
            client=client,
            session_factory=session_factory,
            _repo=MagicMock(),
        )

        with session_factory() as sess:
            assert sess.query(ShortInterestSnapshot).count() == 0

    def test_on_failure_collection_runs_records_failed(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Unexpected error causes collection_runs to record 'failed'."""
        from alphamind.data_sources.finra.short_interest import collect_short_interest

        path = _path_for(_SETTLEMENT_YYYYMMDD)
        client = _make_client({path: RuntimeError("unexpected")})
        mock_repo = MagicMock()

        with pytest.raises(RuntimeError, match="unexpected"):
            collect_short_interest(
                settlement_dates=[_SETTLEMENT_DATE],
                client=client,
                session_factory=session_factory,
                _repo=mock_repo,
            )

        mock_repo.update_failed.assert_called_once()
