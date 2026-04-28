"""Tests for ``q7_cross_asset.compute_intermarket_regime`` — story 08d.

Cover the four intermarket relationships from quant 7d:

- Stocks vs. bonds (SPY/TLT) correlation regime: positive (inflation) vs.
  negative (growth).
- Gold vs. real yields (GLD vs. DFII10): divergence detection.
- Oil vs. energy stock beta (WTI vs. XLE): stability monitoring.
- VIX vs. SPY: divergence flagging (VIX rising on flat/rising market).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7_cross_asset import compute_intermarket_regime
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    MacroObservations,
    OhlcvBars,
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


def _add_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker}",
            asset_class="equity",
            asset_role="universe",
            exchange="NYSE",
            is_active=1,
            added_date="2020-01-01",
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
    _add_ticker(session, ticker)
    for i, close in enumerate(closes):
        ts = (start_day + timedelta(days=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_close(session, ticker=ticker, period_start=ts, close=close)


def _seed_macro(
    session: Session,
    *,
    series_id: str,
    values: list[float],
    start_day: datetime,
    source: str = "FRED",
) -> None:
    for i, v in enumerate(values):
        observation_date = (start_day + timedelta(days=i)).strftime("%Y-%m-%d")
        session.add(
            MacroObservations(
                source=source,
                series_id=series_id,
                observation_date=observation_date,
                revision_number=0,
                release_date=observation_date,
                value=v,
                units="pct",
                frequency="d",
                ingested_at="2026-04-26T00:00:00Z",
            )
        )


# ---------------------------------------------------------------------------
# Stocks vs. bonds regime (SPY/TLT) — positive (inflation) vs. negative (growth)
# ---------------------------------------------------------------------------


class TestIntermarketStocksVsBonds:
    def test_negative_correlation_labeled_growth_environment(self, session: Session) -> None:
        # 60-day window. SPY and TLT move opposite each other on every day →
        # correlation negative → growth environment.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # Build returns and walk closes.
        returns = [(0.01 if i % 2 else -0.01) for i in range(60)]
        spy_closes = [400.0]
        tlt_closes = [100.0]
        for r in returns:
            spy_closes.append(spy_closes[-1] * (1.0 + r))
            tlt_closes.append(tlt_closes[-1] * (1.0 - r))  # opposite direction
        _seed_path(session, ticker="SPY", closes=spy_closes, start_day=start_day)
        _seed_path(session, ticker="TLT", closes=tlt_closes, start_day=start_day)
        # Add stub series for the other intermarket relationships so the
        # block builds.
        _seed_path(session, ticker="GLD", closes=[180.0] * 61, start_day=start_day)
        _seed_path(session, ticker="XLE", closes=[80.0] * 61, start_day=start_day)
        _seed_macro(
            session,
            series_id="DFII10",
            values=[1.5] * 61,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=[70.0] * 61,
            start_day=start_day,
            source="EIA",
        )
        _seed_macro(
            session,
            series_id="VIXCLS",
            values=[15.0] * 61,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        spy_tlt_blocks = [b for b in blocks if b.block_id.endswith("spy_tlt")]
        assert len(spy_tlt_blocks) == 1
        block = spy_tlt_blocks[0]
        assert block.payload["regime_label"] == "growth_environment"
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience

    def test_positive_correlation_labeled_inflation_environment(self, session: Session) -> None:
        # SPY and TLT both rise on every day → positive correlation →
        # inflation environment.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        returns = [(0.01 if i % 2 else -0.01) for i in range(60)]
        spy_closes = [400.0]
        tlt_closes = [100.0]
        for r in returns:
            spy_closes.append(spy_closes[-1] * (1.0 + r))
            tlt_closes.append(tlt_closes[-1] * (1.0 + r))
        _seed_path(session, ticker="SPY", closes=spy_closes, start_day=start_day)
        _seed_path(session, ticker="TLT", closes=tlt_closes, start_day=start_day)
        _seed_path(session, ticker="GLD", closes=[180.0] * 61, start_day=start_day)
        _seed_path(session, ticker="XLE", closes=[80.0] * 61, start_day=start_day)
        _seed_macro(
            session,
            series_id="DFII10",
            values=[1.5] * 61,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=[70.0] * 61,
            start_day=start_day,
            source="EIA",
        )
        _seed_macro(
            session,
            series_id="VIXCLS",
            values=[15.0] * 61,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        spy_tlt_blocks = [b for b in blocks if b.block_id.endswith("spy_tlt")]
        block = spy_tlt_blocks[0]
        assert block.payload["regime_label"] == "inflation_environment"


# ---------------------------------------------------------------------------
# Gold vs. real yields divergence (GLD vs. DFII10)
# ---------------------------------------------------------------------------


class TestIntermarketGoldVsRealYields:
    def test_gold_real_yields_divergence_flag_fires_when_correlation_breaks(
        self, session: Session
    ) -> None:
        # Gold and real-yield divergence: in a textbook regime, GLD and real
        # yields move opposite (negative correlation). When they diverge from
        # that pattern (e.g., both rising), the flag fires.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # Build noisy paths where GLD and DFII10 move together day-by-day.
        # Both rising together → positive correlation → divergence from
        # the textbook negative-correlation regime.
        deltas = [(0.01, 0.02), (-0.005, -0.01), (0.008, 0.015), (-0.003, -0.006)] * 16
        gld_closes = [180.0]
        dfii10_values = [1.5]
        for gld_delta, dfii_delta in deltas[:60]:
            gld_closes.append(gld_closes[-1] * (1.0 + gld_delta))
            dfii10_values.append(dfii10_values[-1] + dfii_delta)
        _seed_path(session, ticker="GLD", closes=gld_closes, start_day=start_day)
        _seed_path(session, ticker="SPY", closes=[400.0] * 61, start_day=start_day)
        _seed_path(session, ticker="TLT", closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker="XLE", closes=[80.0] * 61, start_day=start_day)
        _seed_macro(
            session,
            series_id="DFII10",
            values=dfii10_values,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=[70.0] * 61,
            start_day=start_day,
            source="EIA",
        )
        _seed_macro(
            session,
            series_id="VIXCLS",
            values=[15.0] * 61,
            start_day=start_day,
        )
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        gld_blocks = [b for b in blocks if b.block_id.endswith("gld_real_yields")]
        block = gld_blocks[0]
        # Both rising → positive correlation → divergence from the
        # textbook negative regime → flag.
        names = [flag.name for flag in block.anomaly_flags]
        assert any("gold_real_yields_divergence" in name for name in names), (
            f"expected gold/real-yields divergence flag, got {names}"
        )


# ---------------------------------------------------------------------------
# Oil vs. energy stock beta stability monitoring
# ---------------------------------------------------------------------------


class TestIntermarketOilVsXLEBeta:
    def test_oil_xle_beta_stability_flag_fires_when_beta_drifts(self, session: Session) -> None:
        # Construct a 60-day window where the oil/XLE beta is roughly 1 across
        # the long window, then over the most recent 20 sessions XLE doubles
        # its sensitivity → beta moves >0.5.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # 40 days of beta=1: equal returns.
        early_oil = [(0.01 if i % 2 else -0.01) for i in range(40)]
        early_xle = list(early_oil)
        # 20 days of beta=2: XLE returns twice oil returns.
        late_oil = [(0.01 if i % 2 else -0.01) for i in range(20)]
        late_xle = [r * 2.0 for r in late_oil]
        oil_returns = early_oil + late_oil
        xle_returns = early_xle + late_xle
        oil_closes = [70.0]
        xle_closes = [80.0]
        for r in oil_returns:
            oil_closes.append(oil_closes[-1] * (1.0 + r))
        for r in xle_returns:
            xle_closes.append(xle_closes[-1] * (1.0 + r))
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=oil_closes,
            start_day=start_day,
            source="EIA",
        )
        _seed_path(session, ticker="XLE", closes=xle_closes, start_day=start_day)
        _seed_path(session, ticker="SPY", closes=[400.0] * 61, start_day=start_day)
        _seed_path(session, ticker="TLT", closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker="GLD", closes=[180.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="DFII10", values=[1.5] * 61, start_day=start_day)
        _seed_macro(session, series_id="VIXCLS", values=[15.0] * 61, start_day=start_day)
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        oil_blocks = [b for b in blocks if b.block_id.endswith("oil_xle_beta")]
        block = oil_blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("oil_xle_beta_drift" in name for name in names), (
            f"expected oil/XLE beta-drift flag, got {names}"
        )


# ---------------------------------------------------------------------------
# VIX vs. SPY divergence
# ---------------------------------------------------------------------------


class TestIntermarketVixVsSpy:
    def test_vix_rising_on_flat_market_fires_divergence_flag(self, session: Session) -> None:
        # VIX up day-over-day, SPY up or flat day-over-day → divergence.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # Construct noisy paths where SPY and VIX move together — the
        # textbook regime is negative correlation, so both rising on the
        # same days produces a positive realized correlation → divergence.
        deltas = [(0.005, 0.5), (-0.003, -0.2), (0.004, 0.3), (-0.002, -0.1)] * 16
        spy_closes = [400.0]
        vix_values = [12.0]
        for spy_delta, vix_delta in deltas[:60]:
            spy_closes.append(spy_closes[-1] * (1.0 + spy_delta))
            vix_values.append(vix_values[-1] + vix_delta)
        _seed_path(session, ticker="SPY", closes=spy_closes, start_day=start_day)
        _seed_macro(
            session,
            series_id="VIXCLS",
            values=vix_values,
            start_day=start_day,
        )
        _seed_path(session, ticker="TLT", closes=[100.0] * 61, start_day=start_day)
        _seed_path(session, ticker="GLD", closes=[180.0] * 61, start_day=start_day)
        _seed_path(session, ticker="XLE", closes=[80.0] * 61, start_day=start_day)
        _seed_macro(session, series_id="DFII10", values=[1.5] * 61, start_day=start_day)
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            values=[70.0] * 61,
            start_day=start_day,
            source="EIA",
        )
        session.commit()

        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )

        vix_blocks = [b for b in blocks if b.block_id.endswith("vix_spy")]
        block = vix_blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("vix_spy_divergence" in name for name in names), (
            f"expected VIX/SPY divergence flag, got {names}"
        )
