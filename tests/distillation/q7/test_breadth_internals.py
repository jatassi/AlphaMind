"""Tests for ``q7_cross_asset.compute_breadth_internals`` — story 08d.

Cover the percentage of universe names above EMA, advance/decline within
sectors, and equal-weight vs. cap-weight performance comparison.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import compute_breadth_internals
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OhlcvBars,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def _add_ticker(session: Session, ticker: str, *, sector: str = "tech") -> None:
    asset_id = f"asset-{ticker.lower()}"
    session.add(
        AssetUniverse(
            asset_id=asset_id,
            ticker=ticker,
            full_name=f"{ticker}",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=asset_id,
            alphamind_sector=sector,
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_close(session: Session, *, ticker: str, period_start: str, close: float) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=close,
            adj_high=close,
            adj_low=close,
            adj_close=close,
            adj_volume=1_000_000,
            adj_vwap=close,
            unadj_open=close,
            unadj_high=close,
            unadj_low=close,
            unadj_close=close,
            unadj_volume=1_000_000,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


def _seed_path(
    session: Session,
    *,
    ticker: str,
    closes: list[float],
    start_day: datetime,
) -> None:
    for i, close in enumerate(closes):
        ts = (start_day + timedelta(days=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_close(session, ticker=ticker, period_start=ts, close=close)


# ---------------------------------------------------------------------------
# Percentage above EMA
# ---------------------------------------------------------------------------


class TestBreadthPctAboveEMA:
    def test_pct_above_20day_ema_aggregates_correctly(self, session: Session) -> None:
        # Three tickers in the universe.
        # AAPL: rising path → finishes above 20-day EMA.
        # MSFT: also rising → finishes above 20-day EMA.
        # GOOG: declining path → finishes below 20-day EMA.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=20)

        rising = [100.0 * (1.0 + 0.01 * i) for i in range(21)]
        falling = [120.0 * (1.0 - 0.01 * i) for i in range(21)]

        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_ticker(session, "GOOG", sector="tech")
        _seed_path(session, ticker="AAPL", closes=rising, start_day=start_day)
        _seed_path(session, ticker="MSFT", closes=rising, start_day=start_day)
        _seed_path(session, ticker="GOOG", closes=falling, start_day=start_day)
        # Add SPY for cap-weight reference.
        _add_ticker(session, "SPY", sector="tech")
        _seed_path(session, ticker="SPY", closes=rising, start_day=start_day)
        session.commit()

        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT", "GOOG"),
            sectors=("tech",),
            sector_members={"tech": ("AAPL", "MSFT", "GOOG")},
            broad_market_etf="SPY",
            as_of=as_of,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q7.breadth_internals"
        # Breadth and intermarket carry UNIVERSAL_BROADCAST per the story.
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience

        # 2 of 3 tickers are above 20-day EMA → ~66.7%.
        pct_above = block.payload["pct_above_20d_ema"]
        assert 0.6 < pct_above < 0.7

    def test_advance_decline_per_sector_counts_correctly(self, session: Session) -> None:
        # Two sectors: tech (3 tickers) and financials (2 tickers).
        # Today: AAPL up, MSFT up, GOOG down → tech 2 up, 1 down.
        #        JPM up, BAC down → financials 1 up, 1 down.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        prev_day = as_of - timedelta(days=1)

        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_ticker(session, "GOOG", sector="tech")
        _add_ticker(session, "JPM", sector="financials")
        _add_ticker(session, "BAC", sector="financials")
        _add_ticker(session, "SPY", sector="tech")

        prev_iso = prev_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        as_of_iso = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Previous closes.
        for ticker in ("AAPL", "MSFT", "GOOG", "JPM", "BAC", "SPY"):
            _add_close(session, ticker=ticker, period_start=prev_iso, close=100.0)
        # Today's closes — direction varies.
        _add_close(session, ticker="AAPL", period_start=as_of_iso, close=101.0)
        _add_close(session, ticker="MSFT", period_start=as_of_iso, close=102.0)
        _add_close(session, ticker="GOOG", period_start=as_of_iso, close=99.0)
        _add_close(session, ticker="JPM", period_start=as_of_iso, close=101.0)
        _add_close(session, ticker="BAC", period_start=as_of_iso, close=98.0)
        _add_close(session, ticker="SPY", period_start=as_of_iso, close=101.0)
        session.commit()

        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT", "GOOG", "JPM", "BAC"),
            sectors=("tech", "financials"),
            sector_members={
                "tech": ("AAPL", "MSFT", "GOOG"),
                "financials": ("JPM", "BAC"),
            },
            broad_market_etf="SPY",
            as_of=as_of,
        )

        block = blocks[0]
        ad = block.payload["advance_decline_per_sector"]
        # Sorted by sector key for deterministic rendering.
        assert ad["financials"] == {"advances": 1, "declines": 1}
        assert ad["tech"] == {"advances": 2, "declines": 1}

    def test_equal_vs_cap_weight_compares_today(self, session: Session) -> None:
        # Equal-weight: simple mean of universe daily returns.
        # Cap-weight: SPY daily return.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        prev_day = as_of - timedelta(days=1)

        _add_ticker(session, "AAPL", sector="tech")
        _add_ticker(session, "MSFT", sector="tech")
        _add_ticker(session, "SPY", sector="tech")

        prev_iso = prev_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        as_of_iso = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for ticker in ("AAPL", "MSFT", "SPY"):
            _add_close(session, ticker=ticker, period_start=prev_iso, close=100.0)
        # AAPL +2%, MSFT +4% → equal-weight return = (0.02 + 0.04) / 2 = 0.03.
        _add_close(session, ticker="AAPL", period_start=as_of_iso, close=102.0)
        _add_close(session, ticker="MSFT", period_start=as_of_iso, close=104.0)
        # SPY +1% → cap-weight proxy = 0.01.
        _add_close(session, ticker="SPY", period_start=as_of_iso, close=101.0)
        session.commit()

        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT"),
            sectors=("tech",),
            sector_members={"tech": ("AAPL", "MSFT")},
            broad_market_etf="SPY",
            as_of=as_of,
        )

        block = blocks[0]
        eq_vs_cap = block.payload["equal_vs_cap_weight"]
        assert eq_vs_cap["equal_weight_return"] == pytest.approx(0.03, abs=1e-9)
        assert eq_vs_cap["cap_weight_return"] == pytest.approx(0.01, abs=1e-9)
        assert eq_vs_cap["spread"] == pytest.approx(0.02, abs=1e-9)
