"""Tests for ``q7_cross_asset.compute_correlation_regime_change`` — story 08d.

DB-backed tests. Two named pins (per ALP-797 preserve-list) plus a
minimal loader smoke.  The dispersion-shift, stable-pair, and
narrative-suppression logic is fully pinned by
``test_compute.py::TestCorrelationRegimeChangeCompute``.

Reduced in ALP-797 (q7 pure/DB double-altitude reduction).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import (
    CorrelationRegimeChangeConfig,
    compute_correlation_regime_change,
)
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


# ---------------------------------------------------------------------------
# Correlation breakdown detection — PRESERVE PIN 1 (ALP-797)
# ---------------------------------------------------------------------------


class TestCorrelationBreakdown:
    def test_correlation_breakdown_fires_when_pair_correlation_shifts(
        self, session: Session
    ) -> None:
        # Prior 40-day window: A and B move together (cor ~ +1).
        # Recent 20-day window: B inverts (cor ~ -1) → big Fisher-z shift.
        # The inversion drags long_corr to ~0 across the 60-day window;
        # noise_floor=0 keeps the ALP-541 guard from filtering the synthetic
        # fixture so the breakdown-detection math is the unit under test.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        long_returns_a = [0.01, -0.005, 0.008, -0.012, 0.006] * 8
        long_returns_b = [
            r + (0.00005 if i % 5 else -0.00005) for i, r in enumerate(long_returns_a)
        ]
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b_inverted = [-r for r in short_a]
        a_returns = long_returns_a + short_a
        b_returns = long_returns_b + short_b_inverted
        a_closes = [100.0]
        b_closes = [100.0]
        for r in a_returns:
            a_closes.append(a_closes[-1] * (1.0 + r))
        for r in b_returns:
            b_closes.append(b_closes[-1] * (1.0 + r))
        _seed_path(session, ticker=Symbol("A"), closes=a_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("B"), closes=b_closes, start_day=start_day)
        session.commit()

        blocks = compute_correlation_regime_change(
            session,
            universe_tickers=("A", "B"),
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=20,
                long_window_days=60,
                correlation_breakdown_sigma=1.5,
                correlation_min_overlap_fraction=0.9,
                correlation_noise_floor=0.0,
                correlation_breakdown_fdr_q=1.0,
                dispersion_window_days=20,
                dispersion_sigma=1.5,
                media_silence_hours=12,
                correlation_locus_pair_count_threshold=999,
            ),
        )

        breakdown_blocks = [b for b in blocks if b.block_id.startswith("q7.correlation_breakdown")]
        # When the pair correlation flips, a breakdown block emits.
        assert breakdown_blocks, "expected at least one correlation breakdown block"
        block = breakdown_blocks[0]
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience


# ---------------------------------------------------------------------------
# Narrative lag — correlation breakdown + media silence — PRESERVE PIN 2 (ALP-797)
# ---------------------------------------------------------------------------


class TestNarrativeLagFlag:
    def test_narrative_lag_fires_when_breakdown_and_no_qualifying_news(
        self, session: Session
    ) -> None:
        # Build a correlation breakdown (same fixture as TestCorrelationBreakdown).
        # Don't add any qualifying news articles → narrative_lag_flag fires.
        # noise_floor=0 because the inversion fixture has near-zero long_corr.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        long_returns_a = [0.01, -0.005, 0.008, -0.012, 0.006] * 8
        long_returns_b = [
            r + (0.00005 if i % 5 else -0.00005) for i, r in enumerate(long_returns_a)
        ]
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b_inverted = [-r for r in short_a]
        a_returns = long_returns_a + short_a
        b_returns = long_returns_b + short_b_inverted
        a_closes = [100.0]
        b_closes = [100.0]
        for r in a_returns:
            a_closes.append(a_closes[-1] * (1.0 + r))
        for r in b_returns:
            b_closes.append(b_closes[-1] * (1.0 + r))
        _seed_path(session, ticker=Symbol("A"), closes=a_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("B"), closes=b_closes, start_day=start_day)
        session.commit()

        blocks = compute_correlation_regime_change(
            session,
            universe_tickers=("A", "B"),
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=20,
                long_window_days=60,
                correlation_breakdown_sigma=1.5,
                correlation_min_overlap_fraction=0.9,
                correlation_noise_floor=0.0,
                correlation_breakdown_fdr_q=1.0,
                dispersion_window_days=20,
                dispersion_sigma=1.5,
                media_silence_hours=12,
                correlation_locus_pair_count_threshold=999,
            ),
        )

        narrative_blocks = [b for b in blocks if b.block_id == "q7.narrative_lag"]
        assert narrative_blocks
        block = narrative_blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("narrative_lag_flag" in name for name in names), names
