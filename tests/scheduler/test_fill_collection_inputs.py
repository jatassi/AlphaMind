"""Tests for ``gather_fill_collection_inputs`` (``scheduler.fill_collection_inputs``, story 03b).

The gatherer assembles the typed bundle that ``process_unprocessed_fills``
consumes: ``ca_activities`` from the v1beta1 fetcher, ``alpaca_positions`` /
``alpaca_account`` from the broker adapter, and ``market_inputs`` built from
those positions + the macro-observations table. Per parent decision (H),
any broker-side failure degrades the bundle (returning no-op defaults +
``staleness_flag=True``) rather than aborting the invocation.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import pytest
from alpaca.data.enums import CorporateActionsType
from alpaca.data.models.corporate_actions import CorporateAction
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.money import money, price
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import (
    VenueConfig,
)
from alphamind.execution.broker_adapter.entry_pricing import TouchQuote
from alphamind.execution.broker_adapter.queries import (
    AccountStateQueries,
    OrderSnapshot,
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.persistence.models import AssetUniverse, OhlcvBars
from alphamind.persistence.session import (
    make_engine,
    make_session_factory,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
    SqlOptionsIvProvider,
)
from alphamind.scheduler.invocation import insert_invocation_record
from alphamind.state.invocation_context.context import InvocationHandle
from tests.scheduler.conftest import (
    SHIPPED_CONFIG_DIR,
    _make_venue_config,
)

_NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


@pytest.fixture
def sync_session_factory(
    async_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> Iterator[sessionmaker[Session]]:
    """Sync session factory bound to the same DB as ``async_factory`` (ALP-642).

    ``SqlOptionsIvProvider`` reads ``options_contract_snapshots`` via a
    sync session — depend on ``async_factory`` so the schema and the
    process_lifetimes seed are in place before the sync engine opens.
    """
    del async_factory  # depends-on for ordering only
    db_path = tmp_path / "alphamind.db"
    sync_engine = make_engine(str(db_path))
    try:
        yield make_session_factory(sync_engine)
    finally:
        sync_engine.dispose()


def _make_account_snapshot() -> TradeAccountSnapshot:
    return TradeAccountSnapshot(
        account_id="acc-1",
        cash=money(10_000.0),
        equity=money(10_000.0),
        buying_power=money(10_000.0),
        regt_buying_power=money(10_000.0),
        daytrading_buying_power=money(10_000.0),
        maintenance_margin=money(0.0),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


def _make_position_snapshot(
    symbol: str = "AAPL", current_price_value: float = 150.0
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=10.0,
        avg_entry_price=price(140.0),
        market_value=money(1500.0),
        cost_basis=money(1400.0),
        unrealized_pl=money(100.0),
        unrealized_plpc=0.0714,
        current_price=price(current_price_value),
        side="long",
    )


class _StubQueries:
    """Sync stand-in for ``AccountStateQueries`` that returns canned data."""

    def __init__(
        self,
        *,
        account: TradeAccountSnapshot,
        positions: tuple[PositionSnapshot, ...],
    ) -> None:
        self._account = account
        self._positions = positions

    def get_account(self) -> TradeAccountSnapshot:
        return self._account

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        return self._positions

    async def get_orders(
        self,
        *,
        status: Literal["open", "closed", "all"] = "all",
        since: datetime | None = None,
        until: datetime | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> AsyncIterator[OrderSnapshot]:
        # gather_fill_collection_inputs does not consume orders; the method is present
        # only to satisfy the AccountStateQueriesP protocol surface.
        return
        yield  # pragma: no cover — makes this an async generator


class _StubCorporateActionsQueries:
    """Async stand-in for ``CorporateActionsQueries``."""

    async def get_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...] | None = None,
        start: date | None = None,
        end: date | None = None,
        types: tuple[CorporateActionsType, ...] | None = None,
    ) -> tuple[CorporateAction, ...]:
        return ()


class _StubQuoteSource:
    """Batch quote source stand-in for ``AlpacaQuoteSource`` (ALP-753).

    Returns the configured touch for each requested symbol present in the map
    (others are dropped, mirroring the missing/one-sided/zero semantics), or
    raises ``RuntimeError`` to exercise the universe-layer degradation path.
    """

    def __init__(
        self,
        quotes: Mapping[str, TouchQuote] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self._quotes = dict(quotes or {})
        self._raises = raises
        self.requested: list[str] = []

    async def latest_quotes(self, symbols: Sequence[str]) -> Mapping[str, TouchQuote]:
        self.requested = list(symbols)
        if self._raises:
            msg = "stub batch latest-quote failure"
            raise RuntimeError(msg)
        return {sym: self._quotes[sym] for sym in symbols if sym in self._quotes}


def _no_quotes_factory(
    venue_config: VenueConfig, execution_mode: ExecutionMode
) -> _StubQuoteSource:
    """A quote-source factory that returns no live quotes (pure bar-based layer).

    Injected into integration tests written for the bar-based path so the
    default Alpaca factory's real network fetch is never reached.
    """
    del venue_config, execution_mode
    return _StubQuoteSource({})


async def _open_phase1_handle(
    *,
    async_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
) -> tuple[AsyncSession, InvocationHandle]:
    """Insert the invocation row + open a fresh Phase 1 session.

    Returns ``(session, handle)``; caller is responsible for closing the
    session (use ``async with closing(session)`` or call
    ``await session.close()`` in a finally block).
    """
    from alphamind.config.models.modes import Mode
    from alphamind.config.models.regimes import Regime
    from alphamind.config.models.run_types import RunType
    from alphamind.config.resolver import RuntimeDimensions

    runtime = RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.market_open,
    )
    invocation_id, _ = await insert_invocation_record(
        session_factory=async_factory,
        process_lifetime_id="proc-driver-1",
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="test",
        firing_run_type=RunType.market_open,
        runtime=runtime,
        archive_root=archive_root,
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        now=_NOW,
    )
    session = async_factory()
    return session, InvocationHandle(session=session, invocation_id=invocation_id)


def _make_universe_row(
    ticker: str,
    *,
    is_active: int = 1,
    asset_role: str = "universe",
) -> AssetUniverse:
    """Build a minimal active ``asset_universe`` row for ``ticker``."""
    return AssetUniverse(
        asset_id=f"asset-{ticker.lower()}",
        ticker=ticker,
        full_name=f"{ticker} Inc.",
        asset_class="equity",
        asset_role=asset_role,
        exchange="NASDAQ",
        is_active=is_active,
        added_date="2020-01-01",
        last_updated="2026-05-07T00:00:00Z",
    )


def _make_ohlcv_bar(
    ticker: str,
    *,
    period_start: str,
    unadj_close: float,
    adj_close: float | None = None,
    timeframe: str = "1d",
) -> OhlcvBars:
    """Build an ``ohlcv_bars`` row; ``adj_close`` defaults to ``unadj_close``."""
    adj = unadj_close if adj_close is None else adj_close
    return OhlcvBars(
        ticker=ticker,
        timeframe=timeframe,
        period_start=period_start,
        period_end=period_start,
        session="regular",
        adj_open=adj,
        adj_high=adj,
        adj_low=adj,
        adj_close=adj,
        adj_volume=1_000_000,
        unadj_open=unadj_close,
        unadj_high=unadj_close,
        unadj_low=unadj_close,
        unadj_close=unadj_close,
        unadj_volume=1_000_000,
        source="polygon",
        ingested_at="2026-05-07T00:00:00Z",
    )


class TestReadActiveUniversePrices:
    """``_read_active_universe_prices`` — freshest bar (any timeframe) per active ticker.

    ALP-747: the reference price the decision agents anchor on is the freshest
    recorded bar of *any* timeframe, so a live-tracking intraday close supersedes
    a lagging daily close. Pre-ALP-747 this read only ``1d`` bars (ALP-587).
    """

    async def test_returns_freshest_bar_of_any_timeframe_for_active_tickers(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The freshest bar's ``unadj_close`` is returned per active ticker — an
        intraday bar more recent than the daily close wins (ALP-747); the
        *unadjusted* close is selected, inactive tickers are dropped."""
        from alphamind.scheduler import fill_collection_inputs as module

        async with async_factory() as seed_session:
            seed_session.add_all(
                [
                    _make_universe_row("CSCO"),
                    _make_universe_row("MSFT"),
                    _make_universe_row("INACT", is_active=0),
                ]
            )
            await seed_session.flush()
            seed_session.add_all(
                [
                    # CSCO: two daily bars then a fresher intraday bar. The
                    # intraday close wins (ALP-747); adj_close differs from
                    # unadj_close so the assertion proves the *unadjusted* close
                    # is selected.
                    _make_ohlcv_bar(
                        "CSCO", period_start="2026-05-05T00:00:00+00:00", unadj_close=47.0
                    ),
                    _make_ohlcv_bar(
                        "CSCO",
                        period_start="2026-05-06T00:00:00+00:00",
                        unadj_close=48.5,
                        adj_close=99.0,
                    ),
                    # An intraday bar more recent than the daily close — this is
                    # the freshest reference and must win.
                    _make_ohlcv_bar(
                        "CSCO",
                        period_start="2026-05-07T13:00:00+00:00",
                        unadj_close=50.0,
                        adj_close=101.0,
                        timeframe="1h",
                    ),
                    _make_ohlcv_bar(
                        "MSFT", period_start="2026-05-06T00:00:00+00:00", unadj_close=410.0
                    ),
                    _make_ohlcv_bar(
                        "INACT", period_start="2026-05-06T00:00:00+00:00", unadj_close=12.0
                    ),
                ]
            )
            await seed_session.commit()

        async with async_factory() as session:
            prices = await module._read_active_universe_prices(session, as_of=_NOW)

        assert prices == {"CSCO": 50.0, "MSFT": 410.0}

    async def test_coarser_timeframe_wins_when_period_start_ties(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """On an equal ``period_start`` across timeframes the coarser-grained bar
        wins: its window closes later, so its close is the more recent price.
        Selection stays deterministic and reproducible (ALP-747 AC4)."""
        from alphamind.scheduler import fill_collection_inputs as module

        async with async_factory() as seed_session:
            seed_session.add_all([_make_universe_row("NVDA")])
            await seed_session.flush()
            seed_session.add_all(
                [
                    # Both bars start at the same instant (a top-of-hour open).
                    # The 1h bar's window closes later (14:00 vs 13:15), so its
                    # close is the more recent price and it must win.
                    _make_ohlcv_bar(
                        "NVDA",
                        period_start="2026-05-07T13:00:00+00:00",
                        unadj_close=900.0,
                        timeframe="1h",
                    ),
                    _make_ohlcv_bar(
                        "NVDA",
                        period_start="2026-05-07T13:00:00+00:00",
                        unadj_close=905.0,
                        timeframe="15min",
                    ),
                ]
            )
            await seed_session.commit()

        async with async_factory() as session:
            prices = await module._read_active_universe_prices(session, as_of=_NOW)

        assert prices == {"NVDA": 900.0}

    async def test_drops_stale_bars_past_the_age_bound(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A ticker whose only daily bar is older than the staleness bound is
        omitted — it falls back to the validation tool's UNAVAILABLE path."""
        from alphamind.scheduler import fill_collection_inputs as module

        async with async_factory() as seed_session:
            seed_session.add_all([_make_universe_row("FRESH"), _make_universe_row("STALE")])
            await seed_session.flush()
            seed_session.add_all(
                [
                    _make_ohlcv_bar(
                        "FRESH", period_start="2026-05-06T00:00:00+00:00", unadj_close=20.0
                    ),
                    # ~67 days before _NOW — well past the 7-day bound.
                    _make_ohlcv_bar(
                        "STALE", period_start="2026-03-01T00:00:00+00:00", unadj_close=30.0
                    ),
                ]
            )
            await seed_session.commit()

        async with async_factory() as session:
            prices = await module._read_active_universe_prices(session, as_of=_NOW)

        assert prices == {"FRESH": 20.0}


class TestMergeQuoteAndBarPrices:
    """``_merge_quote_and_bar_prices`` — live quote mid primary, recorded bar fallback.

    ALP-753: every active-universe ticker is anchored on its phase-1 live quote
    mid when one is available, falling back to the freshest recorded bar's
    ``unadj_close`` otherwise; a ticker with neither is omitted entirely.
    """

    def test_quote_mid_primary_bar_fallback_quote_wins_neither_absent(self) -> None:
        from alphamind.scheduler import fill_collection_inputs as module

        active_tickers = ("AAA", "BBB", "CCC", "DDD")
        quotes = {
            # AAA: quote only -> mid (10.00 + 10.04) / 2 = 10.02
            "AAA": TouchQuote(bid=price("10.00"), ask=price("10.04")),
            # CCC: quote present alongside a bar within the divergence bound
            # (mid 100.0 vs bar 99.5, ~0.5% off) -> quote mid wins.
            "CCC": TouchQuote(bid=price("99.98"), ask=price("100.02")),
        }
        # BBB: bar only -> fallback. CCC: bar present but the trusted quote wins.
        bar_prices = {"BBB": 48.5, "CCC": 99.5}

        merged = module._merge_quote_and_bar_prices(
            active_tickers=active_tickers,
            bar_prices=bar_prices,
            quotes=quotes,
        )

        assert merged == {"AAA": 10.02, "BBB": 48.5, "CCC": 100.0}
        assert "DDD" not in merged  # neither quote nor bar

    def test_no_quotes_degrades_to_pure_bar_layer(self) -> None:
        """An empty quote map (the degraded path) reproduces the bar-based
        layer exactly: every active ticker with a bar keeps its close."""
        from alphamind.scheduler import fill_collection_inputs as module

        merged = module._merge_quote_and_bar_prices(
            active_tickers=("AAA", "BBB"),
            bar_prices={"AAA": 47.0, "BBB": 48.5},
            quotes={},
        )

        assert merged == {"AAA": 47.0, "BBB": 48.5}

    def test_divergent_quote_mid_gated_to_bar(self, caplog: pytest.LogCaptureFixture) -> None:
        """ALP-759 — a quote whose mid diverges from the freshest bar beyond the
        bound (MU-style: a $506 mid against a real ~$964 close) is distrusted: the
        recorded bar is used as the reference and a warning is logged. The quote's
        spread is tight here so the divergence check — not the spread floor — is
        the gate that fires."""
        from alphamind.scheduler import fill_collection_inputs as module

        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.fill_collection_inputs"):
            merged = module._merge_quote_and_bar_prices(
                active_tickers=("MU",),
                bar_prices={"MU": 964.0},
                quotes={"MU": TouchQuote(bid=price("505.00"), ask=price("507.00"))},
            )

        assert merged == {"MU": 964.0}  # the bar, not the $506 mid
        assert any(
            "distrusting live quote for MU" in r.message and "diverges" in r.message
            for r in caplog.records
        )

    def test_quote_within_bound_keeps_mid_over_bar(self, caplog: pytest.LogCaptureFixture) -> None:
        """ALP-759 — a quote whose mid is only modestly off the bar (AVGO-style:
        a $457.65 mid vs a $446 bar, ~2.6%, and a ~5.4% spread) passes both
        distrust checks untouched, is still preferred over the bar, and logs no
        distrust warning."""
        from alphamind.scheduler import fill_collection_inputs as module

        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.fill_collection_inputs"):
            merged = module._merge_quote_and_bar_prices(
                active_tickers=("AVGO",),
                bar_prices={"AVGO": 446.0},
                quotes={"AVGO": TouchQuote(bid=price("445.30"), ask=price("470.00"))},
            )

        assert merged == {"AVGO": 457.65}  # quote mid, within bound -> trusted
        assert not any("distrusting" in r.message for r in caplog.records)

    def test_broken_spread_quote_gated_to_bar(self, caplog: pytest.LogCaptureFixture) -> None:
        """ALP-759 — a quote whose mid is within the divergence bound but whose
        relative spread is pathological (INTU-style: a $450 ask vs a $302 bid on a
        ~$331 stock, a ~39% spread while the $376 mid is only ~13.5% off the bar)
        is distrusted by the spread floor and resolves to the recorded bar."""
        from alphamind.scheduler import fill_collection_inputs as module

        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.fill_collection_inputs"):
            merged = module._merge_quote_and_bar_prices(
                active_tickers=("INTU",),
                bar_prices={"INTU": 331.0},
                quotes={"INTU": TouchQuote(bid=price("302.07"), ask=price("450.00"))},
            )

        assert merged == {"INTU": 331.0}  # the bar, not the $376 mid
        assert any(
            "distrusting live quote for INTU" in r.message and "spread" in r.message
            for r in caplog.records
        )

    def test_crossed_quote_gated_by_absolute_spread(self, caplog: pytest.LogCaptureFixture) -> None:
        """ALP-759 — a crossed book (``bid > ask``, the mirror of a broken-side
        IEX quote) yields a negative *signed* spread that an unsigned bound would
        let slip through. The ``|ask - bid|`` distance gates it: a bid $964 / ask
        $52 quote (mid $508) with no bar is distrusted and marked unavailable
        rather than trusted as a $508 reference."""
        from alphamind.scheduler import fill_collection_inputs as module

        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.fill_collection_inputs"):
            merged = module._merge_quote_and_bar_prices(
                active_tickers=("XSED",),
                bar_prices={},
                quotes={"XSED": TouchQuote(bid=price("964.00"), ask=price("52.00"))},
            )

        assert merged == {}  # crossed quote distrusted, no bar -> unavailable
        assert any(
            "distrusting live quote for XSED" in r.message and "spread" in r.message
            for r in caplog.records
        )

    def test_pathological_quote_without_bar_marked_unavailable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ALP-759 — a distrusted quote with no recorded bar to fall back to is
        omitted entirely (the validation tool's ``UNAVAILABLE`` path) rather than
        anchoring the agents on a meaningless mid."""
        from alphamind.scheduler import fill_collection_inputs as module

        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.fill_collection_inputs"):
            merged = module._merge_quote_and_bar_prices(
                active_tickers=("WIDE",),
                bar_prices={},
                quotes={"WIDE": TouchQuote(bid=price("10.00"), ask=price("20.00"))},
            )

        assert merged == {}  # no bar -> unavailable, not the $15 mid
        assert any(
            "distrusting live quote for WIDE" in r.message and "unavailable" in r.message
            for r in caplog.records
        )

    def test_divergence_bound_is_configurable(self) -> None:
        """ALP-759 — the divergence bound is a module default the caller may
        override. A 10%-off quote is trusted under the generous default but gated
        once the caller tightens the bound below that divergence."""
        from alphamind.scheduler import fill_collection_inputs as module

        active_tickers = ("XYZ",)
        # mid 110.0 vs bar 100.0 -> 10% divergence; tight spread.
        quotes = {"XYZ": TouchQuote(bid=price("109.95"), ask=price("110.05"))}
        bar_prices = {"XYZ": 100.0}

        trusted = module._merge_quote_and_bar_prices(
            active_tickers=active_tickers, bar_prices=bar_prices, quotes=quotes
        )
        assert trusted == {"XYZ": 110.0}  # within the 20% default -> quote wins

        gated = module._merge_quote_and_bar_prices(
            active_tickers=active_tickers,
            bar_prices=bar_prices,
            quotes=quotes,
            divergence_tolerance_pct=5.0,
        )
        assert gated == {"XYZ": 100.0}  # 10% > 5% override -> gated to the bar

    def test_spread_bound_is_configurable(self) -> None:
        """ALP-759 — the relative-spread bound is likewise a module default the
        caller may override. A ~15%-spread quote is trusted under the generous 25%
        default but gated once the caller tightens the bound below that spread."""
        from alphamind.scheduler import fill_collection_inputs as module

        active_tickers = ("WIDE",)
        # bid 92.50 / ask 107.50 -> mid 100.0, spread 15/100 = 15%; ~0% divergence.
        quotes = {"WIDE": TouchQuote(bid=price("92.50"), ask=price("107.50"))}
        bar_prices = {"WIDE": 100.0}

        trusted = module._merge_quote_and_bar_prices(
            active_tickers=active_tickers, bar_prices=bar_prices, quotes=quotes
        )
        assert trusted == {"WIDE": 100.0}  # 15% < 25% default -> quote mid wins

        gated = module._merge_quote_and_bar_prices(
            active_tickers=active_tickers,
            bar_prices=bar_prices,
            quotes=quotes,
            max_relative_spread_pct=10.0,
        )
        assert gated == {"WIDE": 100.0}  # 15% > 10% override -> gated; bar == mid here

    def test_gated_counts_reported_disjointly_in_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ALP-759 — the summary line reports five disjoint counts that sum to the
        active total, splitting the two gated outcomes (gated→bar vs gated→
        unavailable) so a distrusted quote with no bar is not double-counted as a
        bar fallback and the unpriced remainder stays accurate."""
        from alphamind.scheduler import fill_collection_inputs as module

        with caplog.at_level(logging.INFO, logger="alphamind.scheduler.fill_collection_inputs"):
            module._merge_quote_and_bar_prices(
                # GOOD: trusted quote. GBAR: divergent quote, has bar -> gated→bar.
                # GNONE: divergent (crossed) quote, no bar -> gated→unavailable.
                # BARONLY: no quote -> bar fallback. NONE: neither -> unpriced.
                active_tickers=("GOOD", "GBAR", "GNONE", "BARONLY", "NONE"),
                bar_prices={"GOOD": 10.0, "GBAR": 964.0, "BARONLY": 5.0},
                quotes={
                    "GOOD": TouchQuote(bid=price("10.00"), ask=price("10.02")),
                    "GBAR": TouchQuote(bid=price("505.00"), ask=price("507.00")),
                    "GNONE": TouchQuote(bid=price("964.00"), ask=price("52.00")),
                },
            )

        summary = next(
            r.getMessage()
            for r in caplog.records
            if "active-universe reference prices" in r.message
        )
        assert "for 5 active ticker(s)" in summary
        assert "1 via live quote" in summary
        assert "1 via recorded-bar fallback" in summary
        assert "1 via recorded bar after distrusting the live quote" in summary
        assert "1 distrusted with no bar to fall back to" in summary
        assert "1 unpriced" in summary


class TestBuildMarketInputs:
    """``_build_market_inputs`` — held-position quotes override universe closes."""

    def test_held_position_price_overrides_universe_close(self) -> None:
        """On overlap the live broker ``current_price`` wins; unheld universe
        tickers still surface from the EOD-close base layer (ALP-587)."""
        from alphamind.scheduler import fill_collection_inputs as module

        positions = (_make_position_snapshot("AAPL", 175.0),)
        market = module._build_market_inputs(
            positions=positions,
            universe_prices={"AAPL": 999.0, "CSCO": 48.5},
            risk_free_rate=0.045,
            as_of=_NOW,
            iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        )

        assert dict(market.underlying_prices) == {"AAPL": 175.0, "CSCO": 48.5}


class TestGatherFillCollectionInputs:
    async def test_returns_populated_bundle_on_happy_path(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """Happy path: alpaca account+positions present, no CA activities, no degradation."""
        from alphamind.scheduler import fill_collection_inputs as module

        positions = (_make_position_snapshot(),)
        account = _make_account_snapshot()

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=account, positions=positions
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
            )
        finally:
            await session.close()

        assert inputs.alpaca_account == account
        assert inputs.alpaca_positions == positions
        assert inputs.ca_activities == ()
        assert isinstance(inputs.market_inputs, MarketInputs)
        assert inputs.market_inputs.underlying_prices["AAPL"] == 150.0
        assert inputs.staleness_flag is False

    async def test_account_runtimeerror_degrades_bundle(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """A ``RuntimeError`` from ``get_account`` returns a bundle with
        ``alpaca_account=None`` and ``staleness_flag=True``; the function does
        not raise."""
        from alphamind.scheduler import fill_collection_inputs as module

        class _FailingQueries:
            def get_account(self) -> TradeAccountSnapshot:
                msg = "alpaca auth failed"
                raise RuntimeError(msg)

            def get_positions(self) -> tuple[PositionSnapshot, ...]:
                return ()

            async def get_orders(
                self,
                *,
                status: Literal["open", "closed", "all"] = "all",
                since: datetime | None = None,
                until: datetime | None = None,
                symbols: tuple[str, ...] | None = None,
            ) -> AsyncIterator[OrderSnapshot]:
                return
                yield  # pragma: no cover — makes this an async generator

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _FailingQueries(),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
            )
        finally:
            await session.close()

        assert inputs.alpaca_account is None
        assert inputs.staleness_flag is True

    async def test_factory_runtimeerror_degrades_alpaca_fields(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """When the broker-adapter factory itself raises (missing creds),
        both account and positions degrade to defaults."""
        from alphamind.scheduler import fill_collection_inputs as module

        def _failing_account_factory(*args: object, **kwargs: object) -> AccountStateQueries:
            msg = "Alpaca paper credentials not set"
            raise RuntimeError(msg)

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=_failing_account_factory,
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
            )
        finally:
            await session.close()

        assert inputs.alpaca_account is None
        assert inputs.alpaca_positions == ()
        assert inputs.staleness_flag is True

    async def test_uses_position_current_prices_for_market_inputs(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """``market_inputs.underlying_prices`` is built from position
        ``current_price`` values when available."""
        from alphamind.scheduler import fill_collection_inputs as module

        positions = (
            _make_position_snapshot("AAPL", 175.0),
            _make_position_snapshot("MSFT", 410.0),
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=_make_account_snapshot(), positions=positions
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
            )
        finally:
            await session.close()

        assert dict(inputs.market_inputs.underlying_prices) == {
            "AAPL": 175.0,
            "MSFT": 410.0,
        }
        assert isinstance(inputs.market_inputs.iv_provider, SqlOptionsIvProvider)
        assert inputs.market_inputs.as_of == _NOW

    async def test_market_inputs_iv_provider_populates_realized_vol_for_open_positions(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """ALP-530 — the IV provider's ``realized_vol`` mapping is populated
        from ``ticker_realized_vol`` for every position underlying that has
        a row in the table. Underlyings without a row are absent from the
        mapping (the consumer's surface->fallback->error chain still
        terminates correctly via the SQL surface lookup + realized-vol
        fallback)."""
        from alphamind.persistence.models import AssetUniverse, TickerRealizedVolRow
        from alphamind.scheduler import fill_collection_inputs as module

        positions = (
            _make_position_snapshot("AAPL", 175.0),
            _make_position_snapshot("MSFT", 410.0),
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        # Seed the realized_vol row tied to this invocation. AAPL needs an
        # asset_universe row for the ticker_realized_vol FK; MSFT is
        # intentionally absent so the assertion that "MSFT not in mapping"
        # validates the per-position filtering.
        async with async_factory() as seed_session:
            seed_session.add(
                AssetUniverse(
                    asset_id="asset-aapl",
                    ticker="AAPL",
                    full_name="Apple Inc.",
                    asset_class="equity",
                    asset_role="universe",
                    exchange="NASDAQ",
                    is_active=1,
                    added_date="2020-01-01",
                    last_updated="2026-05-07T00:00:00Z",
                )
            )
            await seed_session.flush()
            seed_session.add(
                TickerRealizedVolRow(
                    ticker="AAPL",
                    as_of_date="2026-05-07",
                    trailing_30d_realized_vol=0.27,
                    invocation_id=handle.invocation_id,
                    computed_at="2026-05-07T00:00:00Z",
                )
            )
            await seed_session.commit()
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=_make_account_snapshot(), positions=positions
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
                quote_source_factory=_no_quotes_factory,
            )
        finally:
            await session.close()

        iv_provider = inputs.market_inputs.iv_provider
        assert isinstance(iv_provider, SqlOptionsIvProvider)
        # Inspect the internal mapping. ``SqlOptionsIvProvider`` doesn't expose
        # the realized_vol dict on its public surface; the seam below relies
        # on the structural shape established in story 02b / ALP-642.
        realized_vol_map = iv_provider._realized_vol
        assert "AAPL" in realized_vol_map
        assert realized_vol_map["AAPL"].underlying == "AAPL"
        assert realized_vol_map["AAPL"].trailing_30d_realized_vol == pytest.approx(0.27)
        assert "MSFT" not in realized_vol_map

    async def test_market_inputs_covers_unheld_active_universe_ticker(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """ALP-587 — an active-universe ticker that is *not* held surfaces in
        ``underlying_prices`` from its latest EOD bar, so the validation tool
        no longer returns UNAVAILABLE / missing_market_price for it."""
        from alphamind.scheduler import fill_collection_inputs as module

        positions = (_make_position_snapshot("AAPL", 175.0),)

        async with async_factory() as seed_session:
            seed_session.add_all(
                [_make_universe_row("CSCO"), _make_universe_row("INACT", is_active=0)]
            )
            await seed_session.flush()
            seed_session.add_all(
                [
                    _make_ohlcv_bar(
                        "CSCO", period_start="2026-05-06T00:00:00+00:00", unadj_close=48.5
                    ),
                    _make_ohlcv_bar(
                        "INACT", period_start="2026-05-06T00:00:00+00:00", unadj_close=12.0
                    ),
                ]
            )
            await seed_session.commit()

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=_make_account_snapshot(), positions=positions
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
                quote_source_factory=_no_quotes_factory,
            )
        finally:
            await session.close()

        prices = dict(inputs.market_inputs.underlying_prices)
        assert prices["AAPL"] == 175.0
        assert prices["CSCO"] == 48.5
        assert "INACT" not in prices

    async def test_held_position_price_wins_over_eod_bar(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """ALP-587 — when a ticker is both held and present in ``ohlcv_bars``,
        the live broker ``current_price`` overrides the staler EOD close."""
        from alphamind.scheduler import fill_collection_inputs as module

        positions = (_make_position_snapshot("AAPL", 175.0),)

        async with async_factory() as seed_session:
            seed_session.add(_make_universe_row("AAPL"))
            await seed_session.flush()
            seed_session.add(
                _make_ohlcv_bar("AAPL", period_start="2026-05-06T00:00:00+00:00", unadj_close=999.0)
            )
            await seed_session.commit()

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=_make_account_snapshot(), positions=positions
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
                quote_source_factory=_no_quotes_factory,
            )
        finally:
            await session.close()

        assert dict(inputs.market_inputs.underlying_prices)["AAPL"] == 175.0

    async def test_unheld_candidate_anchors_on_live_quote_mid_else_bar(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """ALP-753 — an unheld active-universe ticker with a live quote anchors on
        the captured quote **mid** (not its recorded bar); a ticker with no live
        quote falls back to its freshest recorded bar's ``unadj_close``."""
        from alphamind.scheduler import fill_collection_inputs as module

        async with async_factory() as seed_session:
            seed_session.add_all([_make_universe_row("ORCL"), _make_universe_row("CSCO")])
            await seed_session.flush()
            seed_session.add_all(
                [
                    # ORCL: stale daily bar; a fresh quote must win over it.
                    _make_ohlcv_bar(
                        "ORCL", period_start="2026-05-07T13:00:00+00:00", unadj_close=204.0
                    ),
                    # CSCO: only a bar, no quote -> falls back to the bar.
                    _make_ohlcv_bar(
                        "CSCO", period_start="2026-05-07T13:00:00+00:00", unadj_close=48.5
                    ),
                ]
            )
            await seed_session.commit()

        # ORCL has a live quote (mid 226.10); CSCO has none -> bar fallback.
        quote_source = _StubQuoteSource(
            {"ORCL": TouchQuote(bid=price("226.00"), ask=price("226.20"))}
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=_make_account_snapshot(), positions=()
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
                quote_source_factory=lambda v, m: quote_source,
            )
        finally:
            await session.close()

        prices = dict(inputs.market_inputs.underlying_prices)
        assert prices["ORCL"] == 226.10  # quote mid, not the 204.0 recorded bar
        assert prices["CSCO"] == 48.5  # no quote -> recorded-bar fallback
        assert inputs.staleness_flag is False
        # The whole active set was batch-fetched in one go.
        assert sorted(quote_source.requested) == ["CSCO", "ORCL"]

    async def test_quote_source_runtimeerror_degrades_universe_to_bars(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """ALP-753 — a broker-side failure of the batch fetch degrades the entire
        universe layer to recorded bars and sets ``staleness_flag``; the
        invocation never aborts."""
        from alphamind.scheduler import fill_collection_inputs as module

        async with async_factory() as seed_session:
            seed_session.add_all([_make_universe_row("ORCL"), _make_universe_row("CSCO")])
            await seed_session.flush()
            seed_session.add_all(
                [
                    _make_ohlcv_bar(
                        "ORCL", period_start="2026-05-07T13:00:00+00:00", unadj_close=204.0
                    ),
                    _make_ohlcv_bar(
                        "CSCO", period_start="2026-05-07T13:00:00+00:00", unadj_close=48.5
                    ),
                ]
            )
            await seed_session.commit()

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=_make_account_snapshot(), positions=()
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
                quote_source_factory=lambda v, m: _StubQuoteSource(raises=True),
            )
        finally:
            await session.close()

        prices = dict(inputs.market_inputs.underlying_prices)
        assert prices == {"ORCL": 204.0, "CSCO": 48.5}  # both fell back to bars
        assert inputs.staleness_flag is True

    async def test_held_position_price_wins_over_live_quote_mid(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        env_path: Path,
        archive_root: Path,
    ) -> None:
        """ALP-753 — the held-position overlay still wins on overlap: a held
        ticker keeps its broker ``current_price`` even when a phase-1 live quote
        mid is captured for it in the universe layer."""
        from alphamind.scheduler import fill_collection_inputs as module

        positions = (_make_position_snapshot("AAPL", 175.0),)

        async with async_factory() as seed_session:
            seed_session.add(_make_universe_row("AAPL"))
            await seed_session.flush()
            seed_session.add(
                _make_ohlcv_bar("AAPL", period_start="2026-05-07T13:00:00+00:00", unadj_close=200.0)
            )
            await seed_session.commit()

        # A live quote for AAPL (mid 210.05, within the divergence bound of the
        # 200.0 bar so it is trusted) that the held overlay must still beat.
        quote_source = _StubQuoteSource(
            {"AAPL": TouchQuote(bid=price("210.00"), ask=price("210.10"))}
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_fill_collection_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
                sync_session_factory=sync_session_factory,
                account_queries_factory=lambda v, m: _StubQueries(
                    account=_make_account_snapshot(), positions=positions
                ),
                ca_queries_factory=lambda v, m: _StubCorporateActionsQueries(),
                quote_source_factory=lambda v, m: quote_source,
            )
        finally:
            await session.close()

        # Held current_price (175.0) beats both the quote mid (210.05) and bar (200.0).
        assert dict(inputs.market_inputs.underlying_prices)["AAPL"] == 175.0
