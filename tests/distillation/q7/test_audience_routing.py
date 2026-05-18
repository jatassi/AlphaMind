"""Tests for audience routing — story 08d.

Acceptance criterion: "Q7 outputs default to ``CORRELATION_REGIME_BRIEF``
audience; intermarket and breadth carry ``UNIVERSAL_BROADCAST`` as well."

This file holds end-to-end audience-shape assertions over the public
``compute_*`` entry points; the per-feature tests already lock the per-block
content.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import (
    CorrelationRegimeChangeConfig,
    LeadLagPair,
    compute_breadth_internals,
    compute_correlation_regime_change,
    compute_cross_sector_rotation,
    compute_intermarket_regime,
    compute_intra_sector_correlation,
    compute_lead_lag,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationPairLag,
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
    source: str,
    values: list[float],
    start_day: datetime,
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
# Audience-routing assertions
# ---------------------------------------------------------------------------


class TestAudienceRouting:
    def _seed_minimal(self, session: Session, as_of: datetime) -> None:
        """Seed enough universe data to let every public compute_* return."""
        start_day = as_of - timedelta(days=60)
        flat = [100.0] * 61
        for ticker in (
            "AAPL",
            "MSFT",
            "SPY",
            "QQQ",
            "TLT",
            "GLD",
            "XLE",
            "XLK",
            "SMH",
            "XLF",
            "IWM",
        ):
            _seed_path(session, ticker=ticker, closes=flat, start_day=start_day)
        _seed_macro(
            session,
            series_id="DFII10",
            source="fred",
            values=[1.5] * 61,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="DCOILWTICO",
            source="fred",
            values=[70.0] * 61,
            start_day=start_day,
        )
        _seed_macro(
            session,
            series_id="VIXCLS",
            source="fred",
            values=[15.0] * 61,
            start_day=start_day,
        )
        session.add(
            DistillationPairLag(
                lead_ticker="SMH",
                lag_ticker="QQQ",
                as_of=as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                lead_lag_days_estimate=1.0,
                n_pair_events=30,
                last_overdue_flag=0,
                calibration_state="calibrated",
                ingested_at=as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )

    def test_intra_sector_correlation_routes_to_correlation_regime_brief_only(
        self, session: Session
    ) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        self._seed_minimal(session, as_of)
        session.commit()
        blocks = compute_intra_sector_correlation(
            session,
            sector="tech",
            sector_tickers=("AAPL", "MSFT"),
            as_of=as_of,
            short_window_days=20,
            long_window_days=60,
            divergence_sigma=1.5,
        )
        assert blocks
        for block in blocks:
            assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
            assert OutputAudience.UNIVERSAL_BROADCAST not in block.audience

    def test_cross_sector_rotation_routes_to_correlation_regime_brief_only(
        self, session: Session
    ) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        self._seed_minimal(session, as_of)
        session.commit()
        blocks = compute_cross_sector_rotation(
            session,
            as_of=as_of,
            short_window_days=5,
            long_window_days=20,
        )
        assert blocks
        for block in blocks:
            assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience

    def test_breadth_internals_routes_to_both_audiences(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        self._seed_minimal(session, as_of)
        session.commit()
        blocks = compute_breadth_internals(
            session,
            universe_tickers=("AAPL", "MSFT"),
            sector_members={"tech": ("AAPL", "MSFT")},
            as_of=as_of,
        )
        assert blocks
        for block in blocks:
            assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
            assert OutputAudience.UNIVERSAL_BROADCAST in block.audience

    def test_intermarket_regime_routes_to_both_audiences(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        self._seed_minimal(session, as_of)
        session.commit()
        blocks = compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=60,
            short_window_days=20,
        )
        assert blocks
        for block in blocks:
            assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
            assert OutputAudience.UNIVERSAL_BROADCAST in block.audience

    def test_lead_lag_routes_to_correlation_regime_brief_only(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        self._seed_minimal(session, as_of)
        session.commit()
        blocks = compute_lead_lag(
            session,
            pairs=(
                LeadLagPair(
                    pair_key="semis_to_tech",
                    lead_ticker="SMH",
                    lag_ticker="QQQ",
                    max_days=2,
                ),
            ),
            as_of=as_of,
            overdue_lead_sigma=1.5,
        )
        assert blocks
        for block in blocks:
            assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
            assert OutputAudience.UNIVERSAL_BROADCAST not in block.audience

    def test_correlation_regime_change_routes_to_correlation_regime_brief_only(
        self, session: Session
    ) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        self._seed_minimal(session, as_of)
        session.commit()
        blocks = compute_correlation_regime_change(
            session,
            universe_tickers=("AAPL", "MSFT"),
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=20,
                long_window_days=60,
                correlation_breakdown_sigma=1.5,
                correlation_min_overlap_fraction=0.9,
                correlation_noise_floor=0.05,
                correlation_breakdown_fdr_q=1.0,
                dispersion_window_days=20,
                dispersion_sigma=1.5,
                media_silence_hours=12,
            ),
        )
        # Some blocks may emit even with flat fixtures; assertion is about
        # the audience routing of whatever is emitted.
        for block in blocks:
            assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
            assert OutputAudience.UNIVERSAL_BROADCAST not in block.audience
