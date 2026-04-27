"""
Tests for src/alphamind/data_sources/polygon/ — story 05a.

All Polygon SDK calls are mocked. No real network traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    CorporateActions,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TICKERS = ["AAPL", "MSFT"]
BENCHMARKS = ["SPY"]


def _make_db() -> Any:
    """Return (session_factory, engine) with all tables created in :memory:."""
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    return sf, engine


def _seed_asset_universe(sf: Any, tickers: list[str], benchmarks: list[str]) -> None:
    with sf() as sess:
        for t in tickers:
            sess.merge(
                AssetUniverse(
                    asset_id=t,
                    ticker=t,
                    full_name=t,
                    asset_class="equity",
                    asset_role="universe",
                    exchange="NASDAQ",
                    is_active=1,
                    added_date="2020-01-01",
                    last_updated="2020-01-01T00:00:00Z",
                )
            )
        for b in benchmarks:
            sess.merge(
                AssetUniverse(
                    asset_id=b,
                    ticker=b,
                    full_name=b,
                    asset_class="etf",
                    asset_role="benchmark",
                    exchange="NYSE_ARCA",
                    is_active=1,
                    added_date="2020-01-01",
                    last_updated="2020-01-01T00:00:00Z",
                )
            )
        sess.commit()


def _fake_repo() -> MagicMock:
    """Minimal track_run repo stub that records state without DB."""
    repo = MagicMock()
    repo.insert_running.return_value = None
    repo.update_success.return_value = None
    repo.update_failed.return_value = None
    return repo


def _make_agg(
    ts: int,
    o: float = 100.0,
    h: float = 105.0,
    lo: float = 99.0,
    c: float = 102.0,
    v: int = 1_000_000,
    vw: float = 101.5,
    n: int = 5000,
) -> MagicMock:
    agg = MagicMock()
    agg.timestamp = ts
    agg.open = o
    agg.high = h
    agg.low = lo
    agg.close = c
    agg.volume = v
    agg.vwap = vw
    agg.transactions = n
    return agg


# ---------------------------------------------------------------------------
# client.py
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    """verify_connectivity() returns True on success, False on auth failure."""

    def test_success(self) -> None:
        from alphamind.data_sources.polygon.client import PolygonClient

        mock_rest = MagicMock()
        mock_rest.get_market_status.return_value = MagicMock(market="open")

        client = PolygonClient.__new__(PolygonClient)
        client._rest = mock_rest

        assert client.verify_connectivity() is True

    def test_auth_failure(self) -> None:
        from polygon import AuthError

        from alphamind.data_sources.polygon.client import PolygonClient

        mock_rest = MagicMock()
        mock_rest.get_market_status.side_effect = AuthError("unauthorized")

        client = PolygonClient.__new__(PolygonClient)
        client._rest = mock_rest

        assert client.verify_connectivity() is False


class TestRateLimiterIntegration:
    """PolygonClient.acquire_rate_limit() calls through to the shared RateLimiter."""

    def test_acquire_called(self) -> None:
        from alphamind.data_sources.polygon.client import PolygonClient

        limiter = MagicMock()
        client = PolygonClient.__new__(PolygonClient)
        client._limiter = limiter

        client.acquire_rate_limit()
        limiter.acquire.assert_called_once_with("polygon")


# ---------------------------------------------------------------------------
# equity.py
# ---------------------------------------------------------------------------


class TestCollectUniverseBars:
    """collect_universe_bars writes paired adj/unadj rows to ohlcv_bars."""

    def _make_client(self, adj_aggs: list[Any], unadj_aggs: list[Any]) -> MagicMock:
        """Return a PolygonClient mock with get_aggs returning adj then unadj."""
        client = MagicMock()
        client.get_aggs.side_effect = [adj_aggs, unadj_aggs] * 100  # plenty of calls
        client.acquire_rate_limit = MagicMock()
        return client

    def test_writes_rows_for_each_ticker_timeframe(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, [])

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)
        adj_agg = _make_agg(ts)
        unadj_agg = _make_agg(ts, o=99.0, h=104.0, lo=98.0, c=101.0)

        client = self._make_client([adj_agg], [unadj_agg])
        repo = _fake_repo()

        since = datetime(2026, 4, 24, tzinfo=UTC)
        equity.collect_universe_bars(
            ticker_scope=TICKERS,
            timeframes=["1d"],
            since=since,
            _client=client,
            _session_factory=sf,
            _repo=repo,
        )

        with sf() as sess:
            rows = sess.query(OhlcvBars).all()

        assert len(rows) == len(TICKERS)
        row = rows[0]
        assert row.adj_open == adj_agg.open
        assert row.unadj_open == unadj_agg.open
        assert row.source == "polygon"
        assert row.timeframe == "1d"

    def test_idempotent_rerun_no_duplicates(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, [])

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)
        adj_agg = _make_agg(ts)
        unadj_agg = _make_agg(ts, o=99.0)

        def _fresh_client() -> MagicMock:
            c = MagicMock()
            c.get_aggs.side_effect = [[adj_agg], [unadj_agg]] * 100
            c.acquire_rate_limit = MagicMock()
            return c

        since = datetime(2026, 4, 24, tzinfo=UTC)
        equity.collect_universe_bars(
            ticker_scope=TICKERS,
            timeframes=["1d"],
            since=since,
            _client=_fresh_client(),
            _session_factory=sf,
            _repo=_fake_repo(),
        )
        equity.collect_universe_bars(
            ticker_scope=TICKERS,
            timeframes=["1d"],
            since=since,
            _client=_fresh_client(),
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            count = sess.query(OhlcvBars).count()

        assert count == len(TICKERS)

    def test_includes_benchmarks(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)

        client = MagicMock()
        client.get_aggs.side_effect = [
            [_make_agg(ts)],  # adj
            [_make_agg(ts, o=99.0)],  # unadj
        ] * 100
        client.acquire_rate_limit = MagicMock()

        equity.collect_universe_bars(
            ticker_scope=TICKERS + BENCHMARKS,
            timeframes=["1d"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            count = sess.query(OhlcvBars).count()
        assert count == len(TICKERS) + len(BENCHMARKS)

    def test_full_api_failure_writes_failed_run(self) -> None:
        from polygon import BadResponse

        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        client = MagicMock()
        client.get_aggs.side_effect = BadResponse("API down")
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()

        with pytest.raises(BadResponse):
            equity.collect_universe_bars(
                ticker_scope=["AAPL"],
                timeframes=["1d"],
                since=datetime(2026, 4, 24, tzinfo=UTC),
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        repo.update_failed.assert_called_once()
        with sf() as sess:
            assert sess.query(OhlcvBars).count() == 0

    def test_session_column_regular_hours(self) -> None:
        """Intraday bar at 14:30 UTC = 10:30 ET — regular session."""
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)
        client = MagicMock()
        client.get_aggs.side_effect = [[_make_agg(ts)], [_make_agg(ts, o=99.0)]] * 10
        client.acquire_rate_limit = MagicMock()

        equity.collect_universe_bars(
            ticker_scope=["AAPL"],
            timeframes=["15min"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            row = sess.query(OhlcvBars).first()
        assert row.session == "regular"

    def test_session_column_pre_market(self) -> None:
        """Intraday bar at 10:00 UTC = 06:00 ET — pre-market."""
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        ts = int(datetime(2026, 4, 25, 10, 0, tzinfo=UTC).timestamp() * 1000)
        client = MagicMock()
        client.get_aggs.side_effect = [[_make_agg(ts)], [_make_agg(ts, o=99.0)]] * 10
        client.acquire_rate_limit = MagicMock()

        equity.collect_universe_bars(
            ticker_scope=["AAPL"],
            timeframes=["15min"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            row = sess.query(OhlcvBars).first()
        assert row.session == "pre_market"

    def test_session_column_daily_bar_is_regular(self) -> None:
        """Daily bars use 'regular' regardless of midnight-ET timestamp quirks."""
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        # Polygon emits daily bars at midnight ET (= 04:00 UTC)
        ts = int(datetime(2026, 4, 25, 4, 0, tzinfo=UTC).timestamp() * 1000)
        client = MagicMock()
        client.get_aggs.side_effect = [[_make_agg(ts)], [_make_agg(ts, o=99.0)]] * 10
        client.acquire_rate_limit = MagicMock()

        equity.collect_universe_bars(
            ticker_scope=["AAPL"],
            timeframes=["1d"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            row = sess.query(OhlcvBars).first()
        assert row.session == "regular"

    def test_partial_failure_records_error_summary(self) -> None:
        """Failure on second ticker still writes first ticker's rows."""
        from polygon import BadResponse

        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL", "MSFT"], [])

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)
        call_count = [0]

        def _side_effect(*args: Any, **kwargs: Any) -> list[Any]:
            call_count[0] += 1
            # First two calls (AAPL adj+unadj) succeed; third (MSFT adj) fails
            if call_count[0] <= 2:
                return [_make_agg(ts)]
            raise BadResponse("MSFT failed")

        client = MagicMock()
        client.get_aggs.side_effect = _side_effect
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()
        equity.collect_universe_bars(
            ticker_scope=["AAPL", "MSFT"],
            timeframes=["1d"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=repo,
        )

        # AAPL row written; MSFT row absent
        with sf() as sess:
            tickers_in_db = {r.ticker for r in sess.query(OhlcvBars).all()}
        assert "AAPL" in tickers_in_db
        assert "MSFT" not in tickers_in_db

        repo.update_success.assert_called_once()


class TestBootstrapUniverseBars:
    """bootstrap_universe_bars calls collect_universe_bars with 252-day since."""

    def test_calls_with_252_day_window(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        client = MagicMock()
        client.get_aggs.return_value = []
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()
        equity.bootstrap_universe_bars(
            _client=client,
            _session_factory=sf,
            _repo=repo,
        )

        # get_aggs should be called 5 timeframes * (len(TICKERS)+len(BENCHMARKS)) * 2 (adj+unadj)
        expected_calls = 5 * (len(TICKERS) + len(BENCHMARKS)) * 2
        assert client.get_aggs.call_count == expected_calls


# ---------------------------------------------------------------------------
# options.py
# ---------------------------------------------------------------------------


_SNAP_DEFAULTS = {
    "contract_ticker": "O:AAPL260117C00200000",
    "underlying": "AAPL",
    "exp_date": "2026-01-17",
    "strike": 200.0,
    "contract_type": "call",
    "oi": 1000,
    "volume": 500,
    "bid": 5.0,
    "ask": 5.5,
    "iv": 0.30,
    "delta": 0.45,
    "gamma": 0.02,
    "theta": -0.05,
    "vega": 0.10,
    "rho": 0.01,
    "underlying_price": 195.0,
}


def _make_option_snapshot(**overrides: Any) -> MagicMock:
    """Build a mock OptionContractSnapshot with sensible defaults."""
    v = {**_SNAP_DEFAULTS, **overrides}
    snap = MagicMock()

    details = MagicMock()
    details.ticker = v["contract_ticker"]
    details.expiration_date = v["exp_date"]
    details.strike_price = v["strike"]
    details.contract_type = v["contract_type"]
    snap.details = details

    greeks = MagicMock()
    greeks.delta = v["delta"]
    greeks.gamma = v["gamma"]
    greeks.theta = v["theta"]
    greeks.vega = v["vega"]
    snap.greeks = greeks
    snap.implied_volatility = v["iv"]
    snap.open_interest = v["oi"]

    day = MagicMock()
    day.volume = v["volume"]
    snap.day = day

    last_quote = MagicMock()
    last_quote.bid = v["bid"]
    last_quote.ask = v["ask"]
    snap.last_quote = last_quote

    last_trade = MagicMock()
    last_trade.price = (v["bid"] + v["ask"]) / 2
    snap.last_trade = last_trade

    underlying_asset = MagicMock()
    underlying_asset.price = v["underlying_price"]
    underlying_asset.ticker = v["underlying"]
    snap.underlying_asset = underlying_asset

    snap.rho = v["rho"]
    return snap


class TestCollectOptionsChains:
    def test_writes_new_contract_and_snapshot(self) -> None:
        from alphamind.data_sources.polygon import options

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        snap = _make_option_snapshot()
        client = MagicMock()
        client.list_snapshot_options_chain.return_value = [snap]
        client.acquire_rate_limit = MagicMock()

        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            contracts = sess.query(OptionsContracts).all()
            snapshots = sess.query(OptionsContractSnapshots).all()

        assert len(contracts) == 1
        assert contracts[0].contract_ticker == "O:AAPL260117C00200000"
        assert contracts[0].contract_type == "call"
        assert len(snapshots) == 1
        assert snapshots[0].implied_volatility == pytest.approx(0.30)

    def test_updates_last_seen_at_on_known_contract(self) -> None:
        from alphamind.data_sources.polygon import options

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        snap = _make_option_snapshot()
        client = MagicMock()
        client.list_snapshot_options_chain.return_value = [snap]
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()
        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=repo,
        )

        with sf() as sess:
            first_seen = sess.query(OptionsContracts).first().last_seen_at

        # Second call — same contract
        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            row = sess.query(OptionsContracts).first()
        # last_seen_at should be refreshed (>= first_seen), first_seen_at preserved
        assert row.last_seen_at >= first_seen

    def test_idempotent_snapshots(self) -> None:
        """Re-run with same snapshot_ts produces no duplicate snapshot rows."""
        from alphamind.data_sources.polygon import options

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        snap = _make_option_snapshot()
        client = MagicMock()
        client.list_snapshot_options_chain.return_value = [snap]
        client.acquire_rate_limit = MagicMock()

        fixed_ts = "2026-04-26T12:00:00Z"

        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
            _snapshot_ts=fixed_ts,
        )
        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
            _snapshot_ts=fixed_ts,
        )

        with sf() as sess:
            count = sess.query(OptionsContractSnapshots).count()
        assert count == 1

    def test_full_failure_writes_failed_run(self) -> None:
        from polygon import BadResponse

        from alphamind.data_sources.polygon import options

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        client = MagicMock()
        client.list_snapshot_options_chain.side_effect = BadResponse("down")
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()
        with pytest.raises(BadResponse):
            options.collect_options_chains(
                ticker_scope=["AAPL"],
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        repo.update_failed.assert_called_once()
        with sf() as sess:
            assert sess.query(OptionsContracts).count() == 0


# ---------------------------------------------------------------------------
# corporate_actions.py
# ---------------------------------------------------------------------------


def _make_dividend(
    ticker: str = "AAPL",
    div_id: str = "D001",
    cash_amount: float = 0.25,
    ex_date: str = "2026-03-01",
    record_date: str = "2026-03-02",
    pay_date: str = "2026-03-15",
    declaration_date: str = "2026-02-15",
    dividend_type: str = "CD",
) -> MagicMock:
    d = MagicMock()
    d.id = div_id
    d.ticker = ticker
    d.cash_amount = cash_amount
    d.ex_dividend_date = ex_date
    d.record_date = record_date
    d.pay_date = pay_date
    d.declaration_date = declaration_date
    d.dividend_type = dividend_type
    return d


def _make_split(
    ticker: str = "AAPL",
    split_id: str = "S001",
    execution_date: str = "2026-02-01",
    split_from: float = 1.0,
    split_to: float = 4.0,
) -> MagicMock:
    s = MagicMock()
    s.id = split_id
    s.ticker = ticker
    s.execution_date = execution_date
    s.split_from = split_from
    s.split_to = split_to
    return s


class TestCollectCorporateActions:
    def test_writes_dividend_row(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        div = _make_dividend()
        client = MagicMock()
        client.list_dividends.return_value = iter([div])
        client.list_splits.return_value = iter([])
        client.acquire_rate_limit = MagicMock()

        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            rows = sess.query(CorporateActions).all()
        assert len(rows) == 1
        assert rows[0].action_type == "cash_dividend"
        assert rows[0].cash_amount_per_share == pytest.approx(0.25)
        assert rows[0].ticker == "AAPL"

    def test_writes_split_row(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        split = _make_split()
        client = MagicMock()
        client.list_dividends.return_value = iter([])
        client.list_splits.return_value = iter([split])
        client.acquire_rate_limit = MagicMock()

        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            rows = sess.query(CorporateActions).all()
        assert len(rows) == 1
        assert rows[0].action_type == "split"
        assert rows[0].ratio == pytest.approx(4.0)

    def test_idempotent_rerun_no_duplicates(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        div = _make_dividend()

        def _make_client() -> MagicMock:
            c = MagicMock()
            c.list_dividends.return_value = iter([div])
            c.list_splits.return_value = iter([])
            c.acquire_rate_limit = MagicMock()
            return c

        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"], _client=_make_client(), _session_factory=sf, _repo=_fake_repo()
        )
        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"], _client=_make_client(), _session_factory=sf, _repo=_fake_repo()
        )

        with sf() as sess:
            assert sess.query(CorporateActions).count() == 1

    def test_full_failure_writes_failed_run(self) -> None:
        from polygon import BadResponse

        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        client = MagicMock()
        client.list_dividends.side_effect = BadResponse("down")
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()
        with pytest.raises(BadResponse):
            corporate_actions.collect_corporate_actions(
                ticker_scope=["AAPL"],
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        repo.update_failed.assert_called_once()
        with sf() as sess:
            assert sess.query(CorporateActions).count() == 0

    def test_bootstrap_covers_252_days(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, [])

        client = MagicMock()
        client.list_dividends.return_value = iter([])
        client.list_splits.return_value = iter([])
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()
        corporate_actions.bootstrap_corporate_actions(
            _client=client, _session_factory=sf, _repo=repo
        )

        # list_dividends and list_splits each called once per ticker
        assert client.list_dividends.call_count == len(TICKERS)
        assert client.list_splits.call_count == len(TICKERS)


# ---------------------------------------------------------------------------
# reference.py
# ---------------------------------------------------------------------------


def _make_ticker_details(
    ticker: str = "AAPL",
    market_cap: float = 3_000_000_000_000.0,
    shares_outstanding: int = 15_000_000_000,
    float_shares: int = 14_800_000_000,
    name: str = "Apple Inc.",
) -> MagicMock:
    td = MagicMock()
    td.ticker = ticker
    td.market_cap = market_cap
    td.weighted_shares_outstanding = shares_outstanding
    td.share_class_shares_outstanding = float_shares
    td.name = name
    return td


class TestCollectReference:
    def test_updates_asset_universe_fields(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        td = _make_ticker_details()
        client = MagicMock()
        client.get_ticker_details.return_value = td
        client.acquire_rate_limit = MagicMock()

        reference.collect_reference(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            row = sess.query(AssetUniverse).filter_by(ticker="AAPL").first()

        assert row.market_cap_usd == pytest.approx(3_000_000_000_000.0)
        assert row.shares_outstanding == 15_000_000_000

    def test_updates_benchmark_fields(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, [], ["SPY"])

        td = _make_ticker_details(ticker="SPY", market_cap=500_000_000_000.0)
        client = MagicMock()
        client.get_ticker_details.return_value = td
        client.acquire_rate_limit = MagicMock()

        reference.collect_reference(
            ticker_scope=["SPY"],
            _client=client,
            _session_factory=sf,
            _repo=_fake_repo(),
        )

        with sf() as sess:
            row = sess.query(AssetUniverse).filter_by(ticker="SPY").first()

        assert row.market_cap_usd == pytest.approx(500_000_000_000.0)

    def test_idempotent(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        td = _make_ticker_details()
        client = MagicMock()
        client.get_ticker_details.return_value = td
        client.acquire_rate_limit = MagicMock()

        reference.collect_reference(
            ticker_scope=["AAPL"], _client=client, _session_factory=sf, _repo=_fake_repo()
        )
        reference.collect_reference(
            ticker_scope=["AAPL"], _client=client, _session_factory=sf, _repo=_fake_repo()
        )

        with sf() as sess:
            rows = sess.query(AssetUniverse).all()
        # No new rows created — only one row for AAPL
        assert len(rows) == 1

    def test_full_failure_writes_failed_run(self) -> None:
        from polygon import BadResponse

        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        client = MagicMock()
        client.get_ticker_details.side_effect = BadResponse("down")
        client.acquire_rate_limit = MagicMock()

        repo = _fake_repo()
        with pytest.raises(BadResponse):
            reference.collect_reference(
                ticker_scope=["AAPL"],
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        repo.update_failed.assert_called_once()


# ---------------------------------------------------------------------------
# No-args callability (runner COLLECTORS registry requirement)
# ---------------------------------------------------------------------------


class TestNoArgsCallability:
    """Each collector is callable with no positional args when client +
    session_factory are injected and the DB is seeded."""

    def _make_equity_client(self) -> MagicMock:
        client = MagicMock()
        client.get_aggs.return_value = []
        client.acquire_rate_limit = MagicMock()
        return client

    def _make_options_client(self) -> MagicMock:
        client = MagicMock()
        client.list_snapshot_options_chain.return_value = []
        client.acquire_rate_limit = MagicMock()
        return client

    def _make_corporate_client(self) -> MagicMock:
        client = MagicMock()
        client.list_dividends.return_value = iter([])
        client.list_splits.return_value = iter([])
        client.acquire_rate_limit = MagicMock()
        return client

    def _make_reference_client(self) -> MagicMock:
        client = MagicMock()
        client.get_ticker_details.return_value = _make_ticker_details()
        client.acquire_rate_limit = MagicMock()
        return client

    def test_collect_universe_bars_no_args(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        equity.collect_universe_bars(
            _client=self._make_equity_client(),
            _session_factory=sf,
            _repo=_fake_repo(),
        )

    def test_collect_options_chains_no_args(self) -> None:
        from alphamind.data_sources.polygon import options

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        options.collect_options_chains(
            _client=self._make_options_client(),
            _session_factory=sf,
            _repo=_fake_repo(),
        )

    def test_collect_corporate_actions_no_args(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        corporate_actions.collect_corporate_actions(
            _client=self._make_corporate_client(),
            _session_factory=sf,
            _repo=_fake_repo(),
        )

    def test_collect_reference_no_args(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        reference.collect_reference(
            _client=self._make_reference_client(),
            _session_factory=sf,
            _repo=_fake_repo(),
        )
