"""Tests for ``q7_cross_asset.compute_correlation_regime_change`` — story 08d.

Cover the correlation breakdown detection (per-pair correlation moves
exceeding the configured sigma multiple), the dispersion-shift detection
(universe-wide return stdev rising), and the narrative-lag indicator that
fires when a correlation breakdown coincides with media silence.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7_cross_asset import (
    NARRATIVE_LAG_REGIME_TAGS,
    CorrelationRegimeChangeConfig,
    compute_correlation_regime_change,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    NewsArticles,
    NewsArticleTickers,
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


def _add_article(
    session: Session,
    *,
    article_id: str,
    published_at: datetime,
    ticker: str,
    topic_tags: str | None = None,
) -> None:
    session.add(
        NewsArticles(
            article_id=article_id,
            source="test",
            language="en",
            headline_text=f"headline {article_id}",
            published_at=published_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ingested_at="2026-04-26T00:00:00Z",
            topic_tags=topic_tags,
        )
    )
    session.flush()
    session.add(
        NewsArticleTickers(
            article_id=article_id,
            ticker=ticker,
            is_primary=1,
        )
    )


# ---------------------------------------------------------------------------
# Correlation breakdown detection
# ---------------------------------------------------------------------------


class TestCorrelationBreakdown:
    def test_correlation_breakdown_fires_when_pair_correlation_shifts(
        self, session: Session
    ) -> None:
        # Long window: A and B move together (cor ~ 1).
        # Short window: B inverts (cor ~ -1) → big shift.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=60)
        # 40 long-window days where A and B move identically each day →
        # rolling correlation == 1.0 with zero variance — too tight for
        # a meaningful sigma. Sprinkle small perturbations so the rolling
        # pstdev is nonzero but bounded; then in the short window B
        # inverts sharply so the deviation overwhelms the trailing sigma.
        long_returns_a = [0.01, -0.005, 0.008, -0.012, 0.006] * 8
        # Tiny noise so rolling correlations don't all equal 1.
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
        # Walk closes.
        a_closes = [100.0]
        b_closes = [100.0]
        for r in a_returns:
            a_closes.append(a_closes[-1] * (1.0 + r))
        for r in b_returns:
            b_closes.append(b_closes[-1] * (1.0 + r))
        _seed_path(session, ticker="A", closes=a_closes, start_day=start_day)
        _seed_path(session, ticker="B", closes=b_closes, start_day=start_day)
        session.commit()

        blocks = compute_correlation_regime_change(
            session,
            universe_tickers=("A", "B"),
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=20,
                long_window_days=60,
                correlation_shift_sigma=1.5,
                dispersion_window_days=20,
                dispersion_sigma=1.5,
                media_silence_hours=12,
            ),
        )

        breakdown_blocks = [b for b in blocks if b.block_id.startswith("q7.correlation_breakdown")]
        # When the pair correlation flips, a breakdown block emits.
        assert breakdown_blocks, "expected at least one correlation breakdown block"
        block = breakdown_blocks[0]
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience


# ---------------------------------------------------------------------------
# Dispersion shift
# ---------------------------------------------------------------------------


class TestDispersionShift:
    def test_dispersion_shift_fires_when_cross_stock_stdev_rises(self, session: Session) -> None:
        # Universe of three tickers. For most days, daily returns cluster
        # tightly with tiny natural noise; on the final day, cross-ticker
        # dispersion explodes.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=21)

        # 20 calm days: per-ticker daily returns differ by small offsets
        # so the cross-ticker stdev is small but nonzero each day.
        offsets = [(0.005, 0.006, 0.004), (0.004, 0.005, 0.006),
                   (0.006, 0.004, 0.005), (0.005, 0.005, 0.006),
                   (0.004, 0.006, 0.005)] * 4  # fmt: skip
        a_closes = [100.0]
        b_closes = [200.0]
        c_closes = [50.0]
        for ra, rb, rc in offsets:
            a_closes.append(a_closes[-1] * (1.0 + ra))
            b_closes.append(b_closes[-1] * (1.0 + rb))
            c_closes.append(c_closes[-1] * (1.0 + rc))
        # Day 20: dispersion explodes.
        a_closes.append(a_closes[-1] * 1.10)  # +10%
        b_closes.append(b_closes[-1] * 0.90)  # -10%
        c_closes.append(c_closes[-1] * 1.005)

        _seed_path(session, ticker="A", closes=a_closes, start_day=start_day)
        _seed_path(session, ticker="B", closes=b_closes, start_day=start_day)
        _seed_path(session, ticker="C", closes=c_closes, start_day=start_day)
        session.commit()

        blocks = compute_correlation_regime_change(
            session,
            universe_tickers=("A", "B", "C"),
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=20,
                long_window_days=60,
                correlation_shift_sigma=1.5,
                dispersion_window_days=20,
                dispersion_sigma=1.5,
                media_silence_hours=12,
            ),
        )

        dispersion_blocks = [
            b for b in blocks if b.block_id == "q7.correlation_breakdown.dispersion_shift"
        ]
        assert dispersion_blocks, "expected dispersion shift block"
        block = dispersion_blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("dispersion_shift_flag" in name for name in names), names


# ---------------------------------------------------------------------------
# Narrative lag — correlation breakdown + media silence
# ---------------------------------------------------------------------------


class TestNarrativeLagFlag:
    def test_narrative_lag_fires_when_breakdown_and_no_qualifying_news(
        self, session: Session
    ) -> None:
        # Build a correlation breakdown (same fixture as TestCorrelationBreakdown).
        # Don't add any qualifying news articles → narrative_lag_flag fires.
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
        _seed_path(session, ticker="A", closes=a_closes, start_day=start_day)
        _seed_path(session, ticker="B", closes=b_closes, start_day=start_day)
        session.commit()

        blocks = compute_correlation_regime_change(
            session,
            universe_tickers=("A", "B"),
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=20,
                long_window_days=60,
                correlation_shift_sigma=1.5,
                dispersion_window_days=20,
                dispersion_sigma=1.5,
                media_silence_hours=12,
            ),
        )

        narrative_blocks = [b for b in blocks if b.block_id == "q7.narrative_lag"]
        assert narrative_blocks
        block = narrative_blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("narrative_lag_flag" in name for name in names), names

    def test_narrative_lag_suppressed_when_qualifying_news_present(self, session: Session) -> None:
        # Same correlation breakdown fixture, but with a qualifying news
        # article (topic in the regime-relevant set, ticker overlap with the
        # universe) inside the silence window → narrative_lag suppressed.
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
        _seed_path(session, ticker="A", closes=a_closes, start_day=start_day)
        _seed_path(session, ticker="B", closes=b_closes, start_day=start_day)
        # Qualifying article — topic_tags includes a regime-relevant tag,
        # ticker A is in universe, published within the silence window.
        # Use a topic from the regime-relevant set.
        regime_tag = next(iter(NARRATIVE_LAG_REGIME_TAGS))
        _add_article(
            session,
            article_id="art-1",
            published_at=as_of - timedelta(hours=1),
            ticker="A",
            topic_tags=regime_tag,
        )
        session.commit()

        blocks = compute_correlation_regime_change(
            session,
            universe_tickers=("A", "B"),
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=20,
                long_window_days=60,
                correlation_shift_sigma=1.5,
                dispersion_window_days=20,
                dispersion_sigma=1.5,
                media_silence_hours=12,
            ),
        )

        narrative_blocks = [b for b in blocks if b.block_id == "q7.narrative_lag"]
        if narrative_blocks:
            block = narrative_blocks[0]
            names = [flag.name for flag in block.anomaly_flags]
            assert not any("narrative_lag_flag" in name for name in names), (
                f"unexpected narrative_lag_flag despite qualifying news: {names}"
            )
