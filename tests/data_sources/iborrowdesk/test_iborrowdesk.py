"""
Tests for src/alphamind/data_sources/iborrowdesk/ — story 05l.

All HTTP calls are mocked. No real network access.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.persistence.models import (
    Base,
    BorrowCostDaily,
    BorrowCostIntraday,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_COLLECTOR = "alphamind.data_sources.iborrowdesk.borrow_cost"
_FETCH_TICKER = f"{_COLLECTOR}._fetch_ticker"
_GET_TICKERS = f"{_COLLECTOR}._get_tickers"
_NOW = f"{_COLLECTOR}._now"


def _make_db() -> tuple[Engine, type[Session]]:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def _seed_universe(sf: type[Session], tickers: list[str]) -> None:
    """Insert minimal asset_universe rows so FK constraints hold."""
    from alphamind.persistence.models import AssetUniverse

    with sf() as sess:
        for t in tickers:
            if sess.get(AssetUniverse, t) is None:
                sess.add(
                    AssetUniverse(
                        asset_id=f"id-{t}",
                        ticker=t,
                        full_name=t,
                        asset_class="equity",
                        asset_role="universe",
                        exchange="NASDAQ",
                        is_active=1,
                        added_date="2026-01-01",
                        last_updated="2026-01-01T00:00:00Z",
                    )
                )
        sess.commit()


def _make_response(*, ticker: str = "AAPL") -> dict[str, Any]:
    """Minimal iBorrowDesk JSON response."""
    return {
        "ticker": ticker,
        "cusip": "037833100",
        "daily": [
            {
                "date": "2026-04-25",
                "fee": 0.25,
                "rebate": -0.15,
                "available": 5_000_000,
                "high_fee": 0.30,
                "low_fee": 0.20,
                "high_available": 6_000_000,
                "low_available": 4_000_000,
            }
        ],
        "real_time": [
            {
                "datetime": "2026-04-25T15:45:00Z",
                "fee": 0.27,
                "available": 4_800_000,
            }
        ],
    }


class _FakeRunRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
        self.rows[run_id] = {
            "run_id": run_id,
            "collector": collector,
            "started_at": started_at,
            "status": "running",
            "completed_at": None,
            "rows_written": None,
            "error_summary": None,
        }

    def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
        self.rows[run_id].update(
            status="success", completed_at=completed_at, rows_written=rows_written
        )

    def update_failed(self, run_id: str, error_summary: str) -> None:
        self.rows[run_id].update(status="failed", error_summary=error_summary)


_NOW_TS = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tracer bullet: client sends browser User-Agent header
# ---------------------------------------------------------------------------


class TestClientUserAgent:
    def test_user_agent_is_browser_like(self) -> None:
        """Every request must include a browser-like User-Agent."""
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_response()
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        client = IBorrowDeskClient(http_client=mock_http)
        client.fetch_ticker("AAPL")

        call_kwargs = mock_http.get.call_args
        headers = call_kwargs.kwargs.get("headers", {}) or {}
        ua = headers.get("User-Agent", "")
        assert "Mozilla" in ua, f"Expected Mozilla in User-Agent, got: {ua!r}"


# ---------------------------------------------------------------------------
# Redirect following
# ---------------------------------------------------------------------------


class TestRedirectFollowing:
    def test_client_follows_redirects(self) -> None:
        """Client must follow the apex→www 301 redirect."""
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_response()
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        client = IBorrowDeskClient(http_client=mock_http)
        client.fetch_ticker("AAPL")
        # httpx.Client follow_redirects=True means httpx handles it;
        # we verify the client was constructed with follow_redirects=True.
        assert client.follow_redirects is True


# ---------------------------------------------------------------------------
# verify_connectivity
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_on_http_200(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_response()
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        assert IBorrowDeskClient(http_client=mock_http).verify_connectivity() is True

    def test_returns_false_on_connection_error(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        mock_http = MagicMock()
        mock_http.get.side_effect = httpx.ConnectError("unreachable")
        assert IBorrowDeskClient(http_client=mock_http).verify_connectivity() is False


# ---------------------------------------------------------------------------
# IBorrowDeskCoverageError — HTTP 404 not_found
# ---------------------------------------------------------------------------


class TestCoverageError:
    def test_raises_on_404_not_found_body(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskClient,
            IBorrowDeskCoverageError,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"errors": [{"code": "not_found"}]}
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        with pytest.raises(IBorrowDeskCoverageError):
            IBorrowDeskClient(http_client=mock_http).fetch_ticker("UNKN")

    def test_generic_404_does_not_raise_coverage_error(self) -> None:
        """A 404 without the not_found body raises a plain httpx error, not coverage."""
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskClient,
            IBorrowDeskCoverageError,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_resp
        )
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        with pytest.raises(Exception) as exc_info:
            IBorrowDeskClient(http_client=mock_http).fetch_ticker("UNKN")
        assert not isinstance(exc_info.value, IBorrowDeskCoverageError)


# ---------------------------------------------------------------------------
# IBorrowDeskBlockedError — HTTP 444 and TCP empty-reply
# ---------------------------------------------------------------------------


class TestBlockedError:
    def test_raises_on_http_444(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskBlockedError,
            IBorrowDeskClient,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 444
        mock_resp.json.return_value = {}
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        with pytest.raises(IBorrowDeskBlockedError):
            IBorrowDeskClient(http_client=mock_http).fetch_ticker("AAPL")

    def test_raises_on_tcp_empty_reply(self) -> None:
        """RemoteProtocolError with no data simulates TCP empty-reply."""
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskBlockedError,
            IBorrowDeskClient,
        )

        mock_http = MagicMock()
        mock_http.get.side_effect = httpx.RemoteProtocolError(
            "Server disconnected without sending a response"
        )

        with pytest.raises(IBorrowDeskBlockedError):
            IBorrowDeskClient(http_client=mock_http).fetch_ticker("AAPL")

    def test_blocked_error_is_distinct_from_generic_http_error(self) -> None:
        """IBorrowDeskBlockedError should NOT be a subclass of generic HTTP errors."""
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskBlockedError,
            IBorrowDeskCoverageError,
        )

        assert not issubclass(IBorrowDeskBlockedError, IBorrowDeskCoverageError)
        assert not issubclass(IBorrowDeskCoverageError, IBorrowDeskBlockedError)


# ---------------------------------------------------------------------------
# RateLimiter integration
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_client_acquires_rate_limiter_on_fetch(self) -> None:
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        limiter = RateLimiter()
        limiter.set_limit("iborrowdesk", rate_per_minute=12)
        acquired: list[str] = []
        original = limiter.acquire

        def tracking(provider: str) -> None:
            acquired.append(provider)
            original(provider)

        limiter.acquire = tracking

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_response()
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        IBorrowDeskClient(http_client=mock_http, rate_limiter=limiter).fetch_ticker("AAPL")
        assert "iborrowdesk" in acquired


# ---------------------------------------------------------------------------
# collect_borrow_cost — daily rows
# ---------------------------------------------------------------------------


class TestCollectBorrowCostDaily:
    def test_daily_row_written_with_correct_fields(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        with (
            patch(_FETCH_TICKER, return_value=_make_response()),
            patch(_GET_TICKERS, return_value=["AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            row = sess.get(BorrowCostDaily, ("2026-04-25", "AAPL"))
            assert row is not None
            assert row.fee_pct == pytest.approx(0.25)
            assert row.rebate_pct == pytest.approx(-0.15)
            assert row.available_shares == 5_000_000
            assert row.intraday_high_fee_pct == pytest.approx(0.30)
            assert row.intraday_low_fee_pct == pytest.approx(0.20)
            assert row.intraday_high_available_shares == 6_000_000
            assert row.intraday_low_available_shares == 4_000_000
            assert row.source == "iborrowdesk"

    def test_multiple_daily_entries_written(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        resp = _make_response()
        resp["daily"].append(
            {
                "date": "2026-04-24",
                "fee": 0.22,
                "rebate": -0.12,
                "available": 4_500_000,
                "high_fee": 0.25,
                "low_fee": 0.18,
                "high_available": 5_000_000,
                "low_available": 4_000_000,
            }
        )

        with (
            patch(_FETCH_TICKER, return_value=resp),
            patch(_GET_TICKERS, return_value=["AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            rows = sess.query(BorrowCostDaily).filter_by(ticker="AAPL").all()
            assert len(rows) == 2


# ---------------------------------------------------------------------------
# collect_borrow_cost — intraday rows
# ---------------------------------------------------------------------------


class TestCollectBorrowCostIntraday:
    def test_intraday_row_written_with_correct_fields(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        with (
            patch(_FETCH_TICKER, return_value=_make_response()),
            patch(_GET_TICKERS, return_value=["AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            row = sess.get(BorrowCostIntraday, ("2026-04-25T15:45:00Z", "AAPL"))
            assert row is not None
            assert row.fee_pct == pytest.approx(0.27)
            assert row.available_shares == 4_800_000
            assert row.source == "iborrowdesk"

    def test_multiple_intraday_entries_written(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        resp = _make_response()
        resp["real_time"].append(
            {
                "datetime": "2026-04-25T16:01:00Z",
                "fee": 0.28,
                "available": 4_700_000,
            }
        )

        with (
            patch(_FETCH_TICKER, return_value=resp),
            patch(_GET_TICKERS, return_value=["AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            rows = sess.query(BorrowCostIntraday).filter_by(ticker="AAPL").all()
            assert len(rows) == 2


# ---------------------------------------------------------------------------
# collect_borrow_cost — CoverageError → warn-log, continue
# ---------------------------------------------------------------------------


class TestCoverageErrorHandling:
    def test_warn_logs_and_continues_on_coverage_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskCoverageError

        _engine, sf = _make_db()
        _seed_universe(sf, ["UNKN", "AAPL"])

        def _side_effect(ticker: str) -> dict[str, Any]:
            if ticker == "UNKN":
                raise IBorrowDeskCoverageError("UNKN not in coverage")
            return _make_response(ticker="AAPL")

        with (
            patch(_FETCH_TICKER, side_effect=_side_effect),
            patch(_GET_TICKERS, return_value=["UNKN", "AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
            caplog.at_level(logging.WARNING),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        # AAPL rows still written despite UNKN failure
        with sf() as sess:
            rows = sess.query(BorrowCostDaily).filter_by(ticker="AAPL").all()
            assert len(rows) == 1

        assert any("UNKN" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# collect_borrow_cost — BlockedError → halt sweep, mark failed
# ---------------------------------------------------------------------------


class TestBlockedErrorHandling:
    def test_halts_sweep_and_marks_failed_on_blocked(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskBlockedError

        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL", "MSFT"])
        repo = _FakeRunRepo()
        fetched: list[str] = []

        def _side_effect(ticker: str) -> dict[str, Any]:
            fetched.append(ticker)
            if ticker == "AAPL":
                raise IBorrowDeskBlockedError("444 blocked")
            return _make_response(ticker=ticker)

        with (
            patch(_FETCH_TICKER, side_effect=_side_effect),
            patch(_GET_TICKERS, return_value=["AAPL", "MSFT"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            with pytest.raises(IBorrowDeskBlockedError):
                collect_borrow_cost(_session_factory=sf, _repo=repo)

        # MSFT was never fetched — sweep halted
        assert "MSFT" not in fetched

        # collection_runs row records failed
        run = next(iter(repo.rows.values()))
        assert run["status"] == "failed"

    def test_no_data_rows_written_when_blocked_on_first_ticker(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskBlockedError

        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])
        repo = _FakeRunRepo()

        with (
            patch(_FETCH_TICKER, side_effect=IBorrowDeskBlockedError("444")),
            patch(_GET_TICKERS, return_value=["AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            with pytest.raises(IBorrowDeskBlockedError):
                collect_borrow_cost(_session_factory=sf, _repo=repo)

        with sf() as sess:
            assert sess.query(BorrowCostDaily).count() == 0
            assert sess.query(BorrowCostIntraday).count() == 0


# ---------------------------------------------------------------------------
# collect_borrow_cost — malformed JSON
# ---------------------------------------------------------------------------


class TestMalformedJson:
    def test_missing_daily_key_skips_ticker_gracefully(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Response without 'daily'/'real_time' keys is treated as empty — no crash."""
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        with (
            patch(_FETCH_TICKER, return_value={"ticker": "AAPL"}),
            patch(_GET_TICKERS, return_value=["AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            # Should not raise; just writes 0 rows
            collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostDaily).count() == 0


# ---------------------------------------------------------------------------
# Idempotency — re-run produces no duplicate rows
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_rerun_does_not_duplicate_daily_rows(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        for _ in range(3):
            with (
                patch(_FETCH_TICKER, return_value=_make_response()),
                patch(_GET_TICKERS, return_value=["AAPL"]),
                patch(_NOW, return_value=_NOW_TS),
            ):
                from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

                collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostDaily).filter_by(ticker="AAPL").count() == 1

    def test_rerun_does_not_duplicate_intraday_rows(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        for _ in range(3):
            with (
                patch(_FETCH_TICKER, return_value=_make_response()),
                patch(_GET_TICKERS, return_value=["AAPL"]),
                patch(_NOW, return_value=_NOW_TS),
            ):
                from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

                collect_borrow_cost(_session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostIntraday).filter_by(ticker="AAPL").count() == 1


# ---------------------------------------------------------------------------
# refresh_ticker — single-ticker on-demand path
# ---------------------------------------------------------------------------


class TestRefreshTicker:
    def test_refresh_writes_daily_and_intraday_for_single_ticker(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        with (
            patch(_FETCH_TICKER, return_value=_make_response()),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import refresh_ticker

            freshness = refresh_ticker("AAPL", _session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostDaily).filter_by(ticker="AAPL").count() == 1
            assert sess.query(BorrowCostIntraday).filter_by(ticker="AAPL").count() == 1

        # Returns the most recent real_time.datetime
        assert freshness == "2026-04-25T15:45:00Z"

    def test_refresh_acquires_rate_limiter(self) -> None:
        from alphamind.data_sources._common import RateLimiter

        _engine, sf = _make_db()
        _seed_universe(sf, ["AAPL"])

        limiter = RateLimiter()
        limiter.set_limit("iborrowdesk", rate_per_minute=12)
        acquired: list[str] = []
        original = limiter.acquire

        def tracking(provider: str) -> None:
            acquired.append(provider)
            original(provider)

        limiter.acquire = tracking

        with (
            patch(_FETCH_TICKER, return_value=_make_response()),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import refresh_ticker

            refresh_ticker("AAPL", _session_factory=sf, _repo=_FakeRunRepo(), _rate_limiter=limiter)

        assert "iborrowdesk" in acquired


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------


class TestSchedulerRegistration:
    def test_iborrowdesk_borrow_cost_in_collectors(self) -> None:
        from alphamind.collector.scheduler import COLLECTORS

        assert "iborrowdesk.borrow_cost" in COLLECTORS
