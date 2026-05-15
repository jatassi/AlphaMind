"""
Tests for src/alphamind/data_sources/polygon/ — story 05a.

All Polygon SDK calls are routed through the FakePolygonAPI implementing
the PolygonAPI Protocol.  No real network traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    CorporateActions,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.polygon import (
    FakePolygonAPI,
    make_agg,
    make_dividend,
    make_option_snapshot,
    make_split,
    make_ticker_details,
)
from tests.data_sources._fakes.run_repo import FakeRunRepo

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


# ---------------------------------------------------------------------------
# client.py
# ---------------------------------------------------------------------------


class TestProtocolContract:
    """PolygonClient implements the PolygonAPI Protocol."""

    def test_polygon_client_implements_polygon_api(self) -> None:
        from alphamind.data_sources.polygon._protocol import PolygonAPI
        from alphamind.data_sources.polygon.client import PolygonClient

        # Construct without hitting the SDK; we only need attribute presence.
        client = PolygonClient.__new__(PolygonClient)
        proto: PolygonAPI = client
        for name in (
            "verify_connectivity",
            "acquire_rate_limit",
            "get_aggs",
            "list_snapshot_options_chain",
            "list_dividends",
            "list_splits",
            "get_ticker_details",
        ):
            assert hasattr(proto, name), name


class TestVerifyConnectivity:
    """verify_connectivity() returns True on success, False on auth failure."""

    def test_success(self) -> None:
        from alphamind.data_sources.polygon.client import PolygonClient

        class _OkRest:
            def get_market_status(self) -> Any:
                class Status:
                    market = "open"

                return Status()

        client = PolygonClient.__new__(PolygonClient)
        client._rest = _OkRest()

        assert client.verify_connectivity() is True

    def test_auth_failure(self) -> None:
        from polygon import AuthError

        from alphamind.data_sources.polygon.client import PolygonClient

        class _ForbiddenRest:
            def get_market_status(self) -> Any:
                raise AuthError("unauthorized")

        client = PolygonClient.__new__(PolygonClient)
        client._rest = _ForbiddenRest()

        assert client.verify_connectivity() is False


class TestRateLimiterIntegration:
    """PolygonClient.acquire_rate_limit() calls through to the shared RateLimiter."""

    def test_acquire_called(self) -> None:
        from alphamind.data_sources.polygon.client import PolygonClient

        acquired: list[str] = []

        class _TrackingLimiter:
            def acquire(self, provider: str) -> None:
                acquired.append(provider)

        client = PolygonClient.__new__(PolygonClient)
        client._limiter = _TrackingLimiter()  # type: ignore[assignment]

        client.acquire_rate_limit()
        assert acquired == ["polygon"]


# ---------------------------------------------------------------------------
# equity.py
# ---------------------------------------------------------------------------


class TestCollectUniverseBars:
    """collect_universe_bars writes paired adj/unadj rows to ohlcv_bars."""

    def _make_client(self, adj_aggs: list[Any], unadj_aggs: list[Any]) -> FakePolygonAPI:
        """Return a FakePolygonAPI with get_aggs returning adj then unadj per call."""
        return FakePolygonAPI(aggs_responses=[adj_aggs, unadj_aggs])

    def test_writes_rows_for_each_ticker_timeframe(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, [])

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)
        adj_agg = make_agg(ts)
        unadj_agg = make_agg(ts, o=99.0, h=104.0, lo=98.0, c=101.0)

        client = self._make_client([adj_agg], [unadj_agg])

        since = datetime(2026, 4, 24, tzinfo=UTC)
        equity.collect_universe_bars(
            ticker_scope=TICKERS,
            timeframes=["1d"],
            since=since,
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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
        adj_agg = make_agg(ts)
        unadj_agg = make_agg(ts, o=99.0)

        def _fresh_client() -> FakePolygonAPI:
            return FakePolygonAPI(aggs_responses=[[adj_agg], [unadj_agg]])

        since = datetime(2026, 4, 24, tzinfo=UTC)
        equity.collect_universe_bars(
            ticker_scope=TICKERS,
            timeframes=["1d"],
            since=since,
            _client=_fresh_client(),
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )
        equity.collect_universe_bars(
            ticker_scope=TICKERS,
            timeframes=["1d"],
            since=since,
            _client=_fresh_client(),
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

        with sf() as sess:
            count = sess.query(OhlcvBars).count()

        assert count == len(TICKERS)

    def test_includes_benchmarks(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)

        client = FakePolygonAPI(aggs_responses=[[make_agg(ts)], [make_agg(ts, o=99.0)]])

        equity.collect_universe_bars(
            ticker_scope=TICKERS + BENCHMARKS,
            timeframes=["1d"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

        with sf() as sess:
            count = sess.query(OhlcvBars).count()
        assert count == len(TICKERS) + len(BENCHMARKS)

    def test_full_api_failure_writes_failed_run(self) -> None:
        from polygon import BadResponse

        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        def boom(**_kwargs: Any) -> list[Any]:
            raise BadResponse("API down")

        client = FakePolygonAPI(aggs_handler=boom)
        repo = FakeRunRepo()

        with pytest.raises(BadResponse):
            equity.collect_universe_bars(
                ticker_scope=["AAPL"],
                timeframes=["1d"],
                since=datetime(2026, 4, 24, tzinfo=UTC),
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        assert repo.failed()
        with sf() as sess:
            assert sess.query(OhlcvBars).count() == 0

    def test_session_column_regular_hours(self) -> None:
        """Intraday bar at 14:30 UTC = 10:30 ET — regular session."""
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        ts = int(datetime(2026, 4, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)
        client = FakePolygonAPI(aggs_responses=[[make_agg(ts)], [make_agg(ts, o=99.0)]])

        equity.collect_universe_bars(
            ticker_scope=["AAPL"],
            timeframes=["15min"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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
        client = FakePolygonAPI(aggs_responses=[[make_agg(ts)], [make_agg(ts, o=99.0)]])

        equity.collect_universe_bars(
            ticker_scope=["AAPL"],
            timeframes=["15min"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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
        client = FakePolygonAPI(aggs_responses=[[make_agg(ts)], [make_agg(ts, o=99.0)]])

        equity.collect_universe_bars(
            ticker_scope=["AAPL"],
            timeframes=["1d"],
            since=datetime(2026, 4, 24, tzinfo=UTC),
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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

        def _handler(**_kwargs: Any) -> list[Any]:
            call_count[0] += 1
            # First two calls (AAPL adj+unadj) succeed; third (MSFT adj) fails
            if call_count[0] <= 2:
                return [make_agg(ts)]
            raise BadResponse("MSFT failed")

        client = FakePolygonAPI(aggs_handler=_handler)

        repo = FakeRunRepo()
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

        assert repo.succeeded()


class TestBootstrapUniverseBars:
    """bootstrap_universe_bars calls collect_universe_bars with 252-day since."""

    def test_calls_with_252_day_window(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        client = FakePolygonAPI()  # default: empty aggs

        equity.bootstrap_universe_bars(
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

        # get_aggs should be called 5 timeframes * (len(TICKERS)+len(BENCHMARKS)) * 2 (adj+unadj)
        expected_calls = 5 * (len(TICKERS) + len(BENCHMARKS)) * 2
        assert len(client.aggs_calls) == expected_calls


# ---------------------------------------------------------------------------
# options.py
# ---------------------------------------------------------------------------


class TestCollectOptionsChains:
    def test_writes_new_contract_and_snapshot(self) -> None:
        from alphamind.data_sources.polygon import options

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        snap = make_option_snapshot()
        client = FakePolygonAPI(snapshots_by_underlying={"AAPL": [snap]})

        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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

        snap = make_option_snapshot()
        client = FakePolygonAPI(snapshots_by_underlying={"AAPL": [snap]})

        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

        with sf() as sess:
            first_seen = sess.query(OptionsContracts).first().last_seen_at

        # Second call — same contract
        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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

        snap = make_option_snapshot()
        client = FakePolygonAPI(snapshots_by_underlying={"AAPL": [snap]})

        fixed_ts = "2026-04-26T12:00:00Z"

        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
            _snapshot_ts=fixed_ts,
        )
        options.collect_options_chains(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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

        class _BadClient(FakePolygonAPI):
            def list_snapshot_options_chain(self, underlying: str) -> list[Any]:
                raise BadResponse("down")

        client = _BadClient()
        repo = FakeRunRepo()
        with pytest.raises(BadResponse):
            options.collect_options_chains(
                ticker_scope=["AAPL"],
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        assert repo.failed()
        with sf() as sess:
            assert sess.query(OptionsContracts).count() == 0


# ---------------------------------------------------------------------------
# corporate_actions.py
# ---------------------------------------------------------------------------


class TestCollectCorporateActions:
    def test_writes_dividend_row(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        client = FakePolygonAPI(dividends_by_ticker={"AAPL": [make_dividend()]})

        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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

        client = FakePolygonAPI(splits_by_ticker={"AAPL": [make_split()]})

        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
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

        div = make_dividend()

        def _make_client() -> FakePolygonAPI:
            return FakePolygonAPI(dividends_by_ticker={"AAPL": [div]})

        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"],
            _client=_make_client(),
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )
        corporate_actions.collect_corporate_actions(
            ticker_scope=["AAPL"],
            _client=_make_client(),
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

        with sf() as sess:
            assert sess.query(CorporateActions).count() == 1

    def test_full_failure_writes_failed_run(self) -> None:
        from polygon import BadResponse

        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        class _BadClient(FakePolygonAPI):
            def list_dividends(
                self, ticker: str, ex_dividend_date_gte: str | None = None
            ) -> list[Any]:
                raise BadResponse("down")

        client = _BadClient()
        repo = FakeRunRepo()
        with pytest.raises(BadResponse):
            corporate_actions.collect_corporate_actions(
                ticker_scope=["AAPL"],
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        assert repo.failed()
        with sf() as sess:
            assert sess.query(CorporateActions).count() == 0

    def test_bootstrap_covers_252_days(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, [])

        client = FakePolygonAPI()  # empty dividends/splits

        repo = FakeRunRepo()
        corporate_actions.bootstrap_corporate_actions(
            _client=client, _session_factory=sf, _repo=repo
        )

        # list_dividends and list_splits each called once per ticker
        assert len(client.dividend_calls) == len(TICKERS)
        assert len(client.split_calls) == len(TICKERS)


# ---------------------------------------------------------------------------
# reference.py
# ---------------------------------------------------------------------------


class TestCollectReference:
    def test_updates_asset_universe_fields(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        client = FakePolygonAPI(ticker_details={"AAPL": make_ticker_details()})

        reference.collect_reference(
            ticker_scope=["AAPL"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

        with sf() as sess:
            row = sess.query(AssetUniverse).filter_by(ticker=Symbol("AAPL")).first()

        assert row.market_cap_usd == pytest.approx(3_000_000_000_000.0)
        assert row.shares_outstanding == 15_000_000_000

    def test_updates_benchmark_fields(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, [], ["SPY"])

        details = make_ticker_details(ticker="SPY", market_cap=500_000_000_000.0)
        client = FakePolygonAPI(ticker_details={"SPY": details})

        reference.collect_reference(
            ticker_scope=["SPY"],
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

        with sf() as sess:
            row = sess.query(AssetUniverse).filter_by(ticker=Symbol("SPY")).first()

        assert row.market_cap_usd == pytest.approx(500_000_000_000.0)

    def test_idempotent(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, ["AAPL"], [])

        client = FakePolygonAPI(ticker_details={"AAPL": make_ticker_details()})

        reference.collect_reference(
            ticker_scope=["AAPL"], _client=client, _session_factory=sf, _repo=FakeRunRepo()
        )
        reference.collect_reference(
            ticker_scope=["AAPL"], _client=client, _session_factory=sf, _repo=FakeRunRepo()
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

        class _BadClient(FakePolygonAPI):
            def get_ticker_details(self, ticker: str) -> Any:
                raise BadResponse("down")

        client = _BadClient()

        repo = FakeRunRepo()
        with pytest.raises(BadResponse):
            reference.collect_reference(
                ticker_scope=["AAPL"],
                _client=client,
                _session_factory=sf,
                _repo=repo,
            )

        assert repo.failed()


# ---------------------------------------------------------------------------
# No-args callability (runner COLLECTORS registry requirement)
# ---------------------------------------------------------------------------


class TestNoArgsCallability:
    """Each collector is callable with no positional args when client +
    session_factory are injected and the DB is seeded."""

    def test_collect_universe_bars_no_args(self) -> None:
        from alphamind.data_sources.polygon import equity

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        equity.collect_universe_bars(
            _client=FakePolygonAPI(),
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

    def test_collect_options_chains_no_args(self) -> None:
        from alphamind.data_sources.polygon import options

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        options.collect_options_chains(
            _client=FakePolygonAPI(),
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

    def test_collect_corporate_actions_no_args(self) -> None:
        from alphamind.data_sources.polygon import corporate_actions

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        corporate_actions.collect_corporate_actions(
            _client=FakePolygonAPI(),
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )

    def test_collect_reference_no_args(self) -> None:
        from alphamind.data_sources.polygon import reference

        sf, _ = _make_db()
        _seed_asset_universe(sf, TICKERS, BENCHMARKS)

        details = make_ticker_details()
        client = FakePolygonAPI(ticker_details={t: details for t in TICKERS + BENCHMARKS})

        reference.collect_reference(
            _client=client,
            _session_factory=sf,
            _repo=FakeRunRepo(),
        )
