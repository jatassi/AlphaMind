"""Tests for ``q7_cross_asset.compute_cross_sector_rotation`` — story 08d.

Cover the rolling sector-ETF relative-performance ratios over 5- and 20-day
windows, the rotation velocity classification (``slow_regime_shift`` vs.
``sharp_intraday_event_driven``), and the narrative classification
(``rate_driven`` / ``growth_driven`` / ``risk_appetite_driven``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import compute_cross_sector_rotation
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
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


def _seed_etf_path(
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


# Story 08d names XLK / SMH / XLF / XLE for cross-sector rotation. Story §
# rotation narrative classification proxies are IWM (small-cap) and SPY
# (broad market) for risk-appetite, plus the same ETFs for the other
# two narratives.
DEFAULT_SECTOR_ETFS = ("XLK", "SMH", "XLF", "XLE")
DEFAULT_RISK_PROXIES = ("IWM", "SPY")


# ---------------------------------------------------------------------------
# Velocity classification — slow regime shift vs. sharp intraday event
# ---------------------------------------------------------------------------


class TestCrossSectorRotationVelocity:
    def test_gradual_rotation_classified_slow_regime_shift(self, session: Session) -> None:
        # 21 days of data. Each ETF starts at 100. Over the trailing 5-day
        # window, XLK gradually drifts down by 1% / day (so day 0..5 ranking
        # rotates one position) and the others stay flat. Result: slow
        # regime shift.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=20)

        # XLK declines slowly (1% per day for last 5 days).
        xlk_path = [100.0] * 16 + [
            100.0 * (1.0 - 0.01),
            100.0 * (1.0 - 0.02),
            100.0 * (1.0 - 0.03),
            100.0 * (1.0 - 0.04),
            100.0 * (1.0 - 0.05),
        ]
        # SMH/XLF/XLE: flat path so they share the same start/end ratio.
        flat = [100.0] * 21
        _seed_etf_path(session, ticker="XLK", closes=xlk_path, start_day=start_day)
        _seed_etf_path(session, ticker="SMH", closes=flat, start_day=start_day)
        _seed_etf_path(session, ticker="XLF", closes=flat, start_day=start_day)
        _seed_etf_path(session, ticker="XLE", closes=flat, start_day=start_day)
        _seed_etf_path(session, ticker="IWM", closes=flat, start_day=start_day)
        _seed_etf_path(session, ticker="SPY", closes=flat, start_day=start_day)
        session.commit()

        blocks = compute_cross_sector_rotation(
            session,
            sector_etfs=DEFAULT_SECTOR_ETFS,
            risk_proxies=DEFAULT_RISK_PROXIES,
            as_of=as_of,
            short_window_days=5,
            long_window_days=20,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q7.cross_sector_rotation"
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert block.payload["velocity_label"] == "slow_regime_shift"

    def test_sharp_intraday_rotation_classified_event_driven(self, session: Session) -> None:
        # On the final day, XLK collapses 10% while XLE and XLF rally 8%; this
        # single-session move shoves multiple sectors past each other →
        # sharp intraday event driven.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=20)

        flat = [100.0] * 20
        xlk_path = [*flat, 90.0]  # last day -10%
        xle_path = [*flat, 108.0]  # last day +8%
        xlf_path = [*flat, 108.5]  # last day +8.5%
        smh_path = [100.0] * 21

        _seed_etf_path(session, ticker="XLK", closes=xlk_path, start_day=start_day)
        _seed_etf_path(session, ticker="SMH", closes=smh_path, start_day=start_day)
        _seed_etf_path(session, ticker="XLF", closes=xlf_path, start_day=start_day)
        _seed_etf_path(session, ticker="XLE", closes=xle_path, start_day=start_day)
        _seed_etf_path(session, ticker="IWM", closes=smh_path, start_day=start_day)
        _seed_etf_path(session, ticker="SPY", closes=smh_path, start_day=start_day)
        session.commit()

        blocks = compute_cross_sector_rotation(
            session,
            sector_etfs=DEFAULT_SECTOR_ETFS,
            risk_proxies=DEFAULT_RISK_PROXIES,
            as_of=as_of,
            short_window_days=5,
            long_window_days=20,
        )

        block = blocks[0]
        assert block.payload["velocity_label"] == "sharp_intraday_event_driven"


# ---------------------------------------------------------------------------
# Narrative classification — rate / growth / risk-appetite drivers
# ---------------------------------------------------------------------------


class TestCrossSectorRotationNarrative:
    def _seed_three_sector_etfs_plus_proxies(
        self,
        session: Session,
        *,
        as_of: datetime,
        xlk_path: list[float],
        xlf_path: list[float],
        xle_path: list[float],
        smh_path: list[float],
        iwm_path: list[float],
        spy_path: list[float],
    ) -> None:
        start_day = as_of - timedelta(days=len(xlk_path) - 1)
        for ticker, path in (
            ("XLK", xlk_path),
            ("XLF", xlf_path),
            ("XLE", xle_path),
            ("SMH", smh_path),
            ("IWM", iwm_path),
            ("SPY", spy_path),
        ):
            _seed_etf_path(session, ticker=ticker, closes=path, start_day=start_day)

    def test_rate_driven_when_xlf_xlk_pair_dominates(self, session: Session) -> None:
        # XLF up 10%, XLK flat, XLE flat, IWM/SPY flat. Strongest pair move
        # is XLF/XLK → rate_driven.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        flat = [100.0] * 6  # 5 short-window days plus one base
        xlf = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
        self._seed_three_sector_etfs_plus_proxies(
            session,
            as_of=as_of,
            xlk_path=list(flat),
            xlf_path=xlf,
            xle_path=list(flat),
            smh_path=list(flat),
            iwm_path=list(flat),
            spy_path=list(flat),
        )
        session.commit()

        blocks = compute_cross_sector_rotation(
            session,
            sector_etfs=DEFAULT_SECTOR_ETFS,
            risk_proxies=DEFAULT_RISK_PROXIES,
            as_of=as_of,
            short_window_days=5,
            long_window_days=20,
        )
        assert blocks[0].payload["narrative_label"] == "rate_driven"

    def test_growth_driven_when_xle_xlk_pair_dominates(self, session: Session) -> None:
        # XLE up 12%, others flat → XLE/XLK pair dominates → growth_driven.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        flat = [100.0] * 6
        xle = [100.0, 102.0, 104.0, 107.0, 110.0, 112.0]
        self._seed_three_sector_etfs_plus_proxies(
            session,
            as_of=as_of,
            xlk_path=list(flat),
            xlf_path=list(flat),
            xle_path=xle,
            smh_path=list(flat),
            iwm_path=list(flat),
            spy_path=list(flat),
        )
        session.commit()

        blocks = compute_cross_sector_rotation(
            session,
            sector_etfs=DEFAULT_SECTOR_ETFS,
            risk_proxies=DEFAULT_RISK_PROXIES,
            as_of=as_of,
            short_window_days=5,
            long_window_days=20,
        )
        assert blocks[0].payload["narrative_label"] == "growth_driven"

    def test_risk_appetite_driven_when_iwm_spy_pair_dominates(self, session: Session) -> None:
        # IWM up 15%, SPY flat → IWM/SPY ratio dominates → risk_appetite.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        flat = [100.0] * 6
        iwm = [100.0, 103.0, 106.0, 109.0, 112.0, 115.0]
        self._seed_three_sector_etfs_plus_proxies(
            session,
            as_of=as_of,
            xlk_path=list(flat),
            xlf_path=list(flat),
            xle_path=list(flat),
            smh_path=list(flat),
            iwm_path=iwm,
            spy_path=list(flat),
        )
        session.commit()

        blocks = compute_cross_sector_rotation(
            session,
            sector_etfs=DEFAULT_SECTOR_ETFS,
            risk_proxies=DEFAULT_RISK_PROXIES,
            as_of=as_of,
            short_window_days=5,
            long_window_days=20,
        )
        assert blocks[0].payload["narrative_label"] == "risk_appetite_driven"
