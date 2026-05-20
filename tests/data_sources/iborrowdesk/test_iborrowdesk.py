"""
Tests for src/alphamind/data_sources/iborrowdesk/ — story 05l.

All HTTP calls go through httpx.MockTransport.  No real network access.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import (
    Base,
    BorrowCostDaily,
    BorrowCostIntraday,
    BorrowCostSweepCursor,
)
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.run_repo import FakeRunRepo

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_COLLECTOR = "alphamind.data_sources.iborrowdesk.borrow_cost"
_FETCH_TICKER = f"{_COLLECTOR}._fetch_ticker"
_GET_TICKERS = f"{_COLLECTOR}._get_tickers"
_NOW = f"{_COLLECTOR}._now"


def _make_db() -> tuple[Engine, sessionmaker[Session]]:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def _seed_universe(sf: sessionmaker[Session], tickers: list[str]) -> None:
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


_CURSOR_KEY = "iborrowdesk.borrow_cost"


def _seed_cursor(sf: sessionmaker[Session], next_ticker: str | None) -> None:
    """Seed the borrow-cost sweep resume cursor."""
    with sf() as sess:
        sess.add(
            BorrowCostSweepCursor(
                collector=_CURSOR_KEY,
                next_ticker=next_ticker,
                updated_at="2026-04-25T00:00:00+00:00",
            )
        )
        sess.commit()


def _read_cursor(sf: sessionmaker[Session]) -> BorrowCostSweepCursor | None:
    """Return the persisted borrow-cost sweep cursor row (None when absent)."""
    with sf() as sess:
        return sess.get(BorrowCostSweepCursor, _CURSOR_KEY)


def _blocking_fetch(block_on: str) -> Callable[[str], dict[str, Any]]:
    """``_fetch_ticker`` side-effect: serve data for every ticker except
    *block_on*, which raises ``IBorrowDeskBlockedError`` — simulating the
    iBorrowDesk 444 per-IP quota trip."""
    from alphamind.data_sources.iborrowdesk.client import IBorrowDeskBlockedError

    def _side_effect(ticker: str) -> dict[str, Any]:
        if ticker == block_on:
            raise IBorrowDeskBlockedError(f"HTTP 444 from iBorrowDesk for {ticker}")
        return _make_response(ticker=ticker)

    return _side_effect


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


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )


_NOW_TS = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tracer bullet: client sends browser User-Agent header
# ---------------------------------------------------------------------------


class TestClientUserAgent:
    def test_user_agent_is_browser_like(self) -> None:
        """Every request must include a browser-like User-Agent."""
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_make_response())

        client = IBorrowDeskClient(http_client=_http_client(handler))
        client.fetch_ticker("AAPL")

        ua = captured[0].headers.get("user-agent", "")
        assert "Mozilla" in ua, f"Expected Mozilla in User-Agent, got: {ua!r}"


# ---------------------------------------------------------------------------
# Redirect following
# ---------------------------------------------------------------------------


class TestRedirectFollowing:
    def test_client_follows_redirects(self) -> None:
        """Client must follow the apex→www 301 redirect."""
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_response())

        client = IBorrowDeskClient(http_client=_http_client(handler))
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

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_response())

        assert IBorrowDeskClient(http_client=_http_client(handler)).verify_connectivity() is True

    def test_returns_false_on_connection_error(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskClient

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        assert IBorrowDeskClient(http_client=_http_client(handler)).verify_connectivity() is False


# ---------------------------------------------------------------------------
# IBorrowDeskCoverageError — HTTP 404 not_found
# ---------------------------------------------------------------------------


class TestCoverageError:
    def test_raises_on_404_not_found_body(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskClient,
            IBorrowDeskCoverageError,
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"errors": [{"code": "not_found"}]})

        with pytest.raises(IBorrowDeskCoverageError):
            IBorrowDeskClient(http_client=_http_client(handler)).fetch_ticker("UNKN")

    def test_generic_404_does_not_raise_coverage_error(self) -> None:
        """A 404 without the not_found body raises a plain httpx error, not coverage."""
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskClient,
            IBorrowDeskCoverageError,
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={})

        with pytest.raises(Exception) as exc_info:
            IBorrowDeskClient(http_client=_http_client(handler)).fetch_ticker("UNKN")
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

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(444)

        with pytest.raises(IBorrowDeskBlockedError):
            IBorrowDeskClient(http_client=_http_client(handler)).fetch_ticker("AAPL")

    def test_raises_on_tcp_empty_reply(self) -> None:
        """RemoteProtocolError with no data simulates TCP empty-reply."""
        from alphamind.data_sources.iborrowdesk.client import (
            IBorrowDeskBlockedError,
            IBorrowDeskClient,
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response")

        with pytest.raises(IBorrowDeskBlockedError):
            IBorrowDeskClient(http_client=_http_client(handler)).fetch_ticker("AAPL")

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

        limiter.acquire = tracking  # type: ignore[method-assign]  # mock-method assignment

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_response())

        IBorrowDeskClient(http_client=_http_client(handler), rate_limiter=limiter).fetch_ticker(
            "AAPL"
        )
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

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

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

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            rows = sess.query(BorrowCostDaily).filter_by(ticker=Symbol("AAPL")).all()
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

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

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

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            rows = sess.query(BorrowCostIntraday).filter_by(ticker=Symbol("AAPL")).all()
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
            return _make_response(ticker=Symbol("AAPL"))

        with (
            patch(_FETCH_TICKER, side_effect=_side_effect),
            patch(_GET_TICKERS, return_value=["UNKN", "AAPL"]),
            patch(_NOW, return_value=_NOW_TS),
            caplog.at_level(logging.WARNING),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        # AAPL rows still written despite UNKN failure
        with sf() as sess:
            rows = sess.query(BorrowCostDaily).filter_by(ticker=Symbol("AAPL")).all()
            assert len(rows) == 1

        assert any("UNKN" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# collect_borrow_cost — BlockedError → halt sweep, partial-success + cursor
# ---------------------------------------------------------------------------


class TestBlockedErrorHandling:
    """A 444 block halts the sweep but lands a partial-success run carrying a
    resume cursor — it no longer re-raises or marks the run ``failed``."""

    def test_block_halts_sweep_without_fetching_remaining_tickers(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAA", "BBB", "CCC"])
        fetched: list[str] = []

        def _side_effect(ticker: str) -> dict[str, Any]:
            fetched.append(ticker)
            return _blocking_fetch("BBB")(ticker)

        with (
            patch(_FETCH_TICKER, side_effect=_side_effect),
            patch(_GET_TICKERS, return_value=["AAA", "BBB", "CCC"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        # CCC never reached — the sweep halts on the block per the client contract.
        assert fetched == ["AAA", "BBB"]

    def test_block_is_partial_success_not_failed(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAA", "BBB"])
        repo = FakeRunRepo()

        with (
            patch(_FETCH_TICKER, side_effect=_blocking_fetch("BBB")),
            patch(_GET_TICKERS, return_value=["AAA", "BBB"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            # No exception propagates — the block is an expected degraded state.
            collect_borrow_cost(_session_factory=sf, _repo=repo)

        assert repo.succeeded()
        assert not repo.failed()
        summary = repo.latest()["error_summary"]
        assert summary is not None
        assert "BBB" in summary

    def test_block_records_resume_cursor_at_blocked_ticker(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAA", "BBB", "CCC"])

        with (
            patch(_FETCH_TICKER, side_effect=_blocking_fetch("BBB")),
            patch(_GET_TICKERS, return_value=["AAA", "BBB", "CCC"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        cursor = _read_cursor(sf)
        assert cursor is not None
        assert cursor.next_ticker == "BBB"

    def test_rows_swept_before_block_are_committed(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAA", "BBB"])

        with (
            patch(_FETCH_TICKER, side_effect=_blocking_fetch("BBB")),
            patch(_GET_TICKERS, return_value=["AAA", "BBB"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostDaily).filter_by(ticker="AAA").count() == 1
            assert sess.query(BorrowCostDaily).filter_by(ticker="BBB").count() == 0

    def test_block_on_first_ticker_writes_no_rows_but_still_succeeds(self) -> None:
        from alphamind.data_sources.iborrowdesk.client import IBorrowDeskBlockedError

        _engine, sf = _make_db()
        _seed_universe(sf, ["AAA"])
        repo = FakeRunRepo()

        with (
            patch(_FETCH_TICKER, side_effect=IBorrowDeskBlockedError("HTTP 444")),
            patch(_GET_TICKERS, return_value=["AAA"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=repo)

        assert repo.succeeded()
        with sf() as sess:
            assert sess.query(BorrowCostDaily).count() == 0
            assert sess.query(BorrowCostIntraday).count() == 0
        # The blocked ticker is recorded so the next run retries it first.
        cursor = _read_cursor(sf)
        assert cursor is not None
        assert cursor.next_ticker == "AAA"


# ---------------------------------------------------------------------------
# collect_borrow_cost — resumable rotating-cursor sweep
# ---------------------------------------------------------------------------


class TestResumableSweep:
    """Consecutive runs resume from the persisted cursor, rotating coverage
    across the universe instead of restarting at the same head every run."""

    def test_run_resumes_from_persisted_cursor_and_wraps(self) -> None:
        _engine, sf = _make_db()
        universe = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        _seed_universe(sf, universe)
        _seed_cursor(sf, "CCC")
        fetched: list[str] = []

        def _side_effect(ticker: str) -> dict[str, Any]:
            fetched.append(ticker)
            return _make_response(ticker=ticker)

        with (
            patch(_FETCH_TICKER, side_effect=_side_effect),
            patch(_GET_TICKERS, return_value=universe),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        # Sweep starts at the cursor and wraps around the end of the universe.
        assert fetched == ["CCC", "DDD", "EEE", "AAA", "BBB"]

    def test_full_sweep_clears_cursor(self) -> None:
        _engine, sf = _make_db()
        _seed_universe(sf, ["AAA", "BBB", "CCC"])
        _seed_cursor(sf, "BBB")

        with (
            patch(_FETCH_TICKER, return_value=_make_response()),
            patch(_GET_TICKERS, return_value=["AAA", "BBB", "CCC"]),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        # A block-free sweep covered the whole universe — cursor resets.
        cursor = _read_cursor(sf)
        assert cursor is not None
        assert cursor.next_ticker is None

    def test_cursor_pointing_at_inactive_ticker_restarts_from_head(self) -> None:
        _engine, sf = _make_db()
        universe = ["AAA", "BBB", "CCC"]
        _seed_universe(sf, universe)
        _seed_cursor(sf, "ZZZ")  # not in the active universe
        fetched: list[str] = []

        def _side_effect(ticker: str) -> dict[str, Any]:
            fetched.append(ticker)
            return _make_response(ticker=ticker)

        with (
            patch(_FETCH_TICKER, side_effect=_side_effect),
            patch(_GET_TICKERS, return_value=universe),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        assert fetched == ["AAA", "BBB", "CCC"]

    def test_consecutive_blocked_runs_rotate_coverage(self) -> None:
        """Two blocked runs cover disjoint slices — the tail is not stranded."""
        _engine, sf = _make_db()
        universe = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        _seed_universe(sf, universe)

        from alphamind.data_sources.iborrowdesk.borrow_cost import collect_borrow_cost

        # Run 1 blocks at CCC after landing AAA, BBB.
        with (
            patch(_FETCH_TICKER, side_effect=_blocking_fetch("CCC")),
            patch(_GET_TICKERS, return_value=universe),
            patch(_NOW, return_value=_NOW_TS),
        ):
            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        # Run 2 resumes at CCC, blocks at FFF after landing CCC, DDD, EEE.
        with (
            patch(_FETCH_TICKER, side_effect=_blocking_fetch("FFF")),
            patch(_GET_TICKERS, return_value=universe),
            patch(_NOW, return_value=_NOW_TS),
        ):
            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            covered = {t for (t,) in sess.query(BorrowCostDaily.ticker).distinct()}

        assert covered == {"AAA", "BBB", "CCC", "DDD", "EEE"}
        cursor = _read_cursor(sf)
        assert cursor is not None
        assert cursor.next_ticker == "FFF"


# ---------------------------------------------------------------------------
# _get_tickers — deterministic sweep order
# ---------------------------------------------------------------------------


class TestSweepOrdering:
    def test_get_tickers_returns_sorted_order(self) -> None:
        """A deterministic sweep order keeps the rotating resume cursor
        coherent run-to-run."""
        from alphamind.data_sources.iborrowdesk.borrow_cost import _get_tickers

        assert _get_tickers(["MSFT", "AAPL", "SHOP", "GOOG"]) == [
            "AAPL",
            "GOOG",
            "MSFT",
            "SHOP",
        ]


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
            collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

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

                collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostDaily).filter_by(ticker=Symbol("AAPL")).count() == 1

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

                collect_borrow_cost(_session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostIntraday).filter_by(ticker=Symbol("AAPL")).count() == 1


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

            freshness = refresh_ticker("AAPL", _session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            assert sess.query(BorrowCostDaily).filter_by(ticker=Symbol("AAPL")).count() == 1
            assert sess.query(BorrowCostIntraday).filter_by(ticker=Symbol("AAPL")).count() == 1

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

        limiter.acquire = tracking  # type: ignore[method-assign]  # mock-method assignment

        with (
            patch(_FETCH_TICKER, return_value=_make_response()),
            patch(_NOW, return_value=_NOW_TS),
        ):
            from alphamind.data_sources.iborrowdesk.borrow_cost import refresh_ticker

            refresh_ticker(
                "AAPL",
                _session_factory=sf,
                _repo=FakeRunRepo(),
                _rate_limiter=limiter,
            )

        assert "iborrowdesk" in acquired


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------


class TestSchedulerRegistration:
    def test_iborrowdesk_borrow_cost_in_collectors(self) -> None:
        from alphamind.collector.scheduler import COLLECTORS

        assert "iborrowdesk.borrow_cost" in COLLECTORS
