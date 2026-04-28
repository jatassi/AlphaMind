"""Tests for per-ticker sentiment percentile — story 02-distillation-layer/08f.

Cover ``compute_sentiment_percentile``: per-ticker percentile against the
trailing baseline, bootstrap fallback to ``universe_pooled_sentiment_distribution``
when the ticker has fewer than ``sentiment_min_observations``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.qualitative_derived import (
    compute_sentiment_percentile,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationTickerBaseline,
    NewsArticles,
    NewsArticleTickers,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory

SENTIMENT_MIN_OBSERVATIONS = 30
SENTIMENT_BASELINE_DAYS = 60


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


def _add_ticker(session: Session, ticker: str, sector: str = "tech") -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=ticker,
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
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=sector,
            domain_researcher=f"{sector}_researcher",
            sector_etf="XLK",
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_baseline(
    session: Session,
    *,
    ticker: str,
    as_of: str,
    mean: float,
    stdev: float,
    n_observations: int,
    state: CalibrationState = CalibrationState.CALIBRATED,
) -> None:
    session.add(
        DistillationTickerBaseline(
            ticker=ticker,
            baseline_kind="sentiment",
            as_of=as_of,
            mean=mean,
            stdev=stdev,
            n_observations=n_observations,
            window_days=SENTIMENT_BASELINE_DAYS,
            calibration_state=state.value,
            ingested_at=as_of,
        )
    )


def _add_recent_article(
    session: Session,
    *,
    article_id: str,
    ticker: str,
    published_at: str,
    score: float,
) -> None:
    session.add(
        NewsArticles(
            article_id=article_id,
            source="test",
            url=None,
            language="en",
            headline_text=f"news for {ticker}",
            body_path=None,
            published_at=published_at,
            ingested_at=published_at,
        )
    )
    session.flush()
    session.add(
        NewsArticleTickers(
            article_id=article_id,
            ticker=ticker,
            is_primary=1,
            vendor_sentiment_score=score,
            vendor_sentiment_label=None,
        )
    )


# ---------------------------------------------------------------------------
# Calibrated path — per-ticker baseline drives percentile
# ---------------------------------------------------------------------------


class TestSentimentPercentileCalibrated:
    def test_current_reading_above_baseline_mean_yields_high_percentile(
        self, session: Session
    ) -> None:
        _add_ticker(session, "AAPL")
        # Baseline mean=0.0, stdev=0.1 with 50 observations (above min)
        _add_baseline(
            session,
            ticker="AAPL",
            as_of="2026-04-25T00:00:00Z",
            mean=0.0,
            stdev=0.1,
            n_observations=50,
        )
        # Current reading: 0.20 = 2 stdev above mean → ~97.7 percentile
        _add_recent_article(
            session,
            article_id="art-1",
            ticker="AAPL",
            published_at="2026-04-26T01:00:00Z",
            score=0.20,
        )
        session.commit()

        blocks = compute_sentiment_percentile(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T03:00:00Z",
            sentiment_min_observations=SENTIMENT_MIN_OBSERVATIONS,
        )
        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "qual.sentiment_percentile"
        assert OutputAudience.SECTOR_TECH_SEMIS in block.audience
        assert block.calibration_state is CalibrationState.CALIBRATED
        per_ticker = block.payload["per_ticker"]
        assert "AAPL" in per_ticker
        entry = per_ticker["AAPL"]
        assert entry["calibration_state"] == "calibrated"
        # 2 stdev → 97.7 percentile
        assert entry["percentile"] == pytest.approx(97.72, abs=0.5)
        assert entry["current_mean"] == pytest.approx(0.20)

    def test_current_reading_at_baseline_mean_yields_50th_percentile(
        self, session: Session
    ) -> None:
        _add_ticker(session, "AAPL")
        _add_baseline(
            session,
            ticker="AAPL",
            as_of="2026-04-25T00:00:00Z",
            mean=0.5,
            stdev=0.1,
            n_observations=50,
        )
        _add_recent_article(
            session,
            article_id="art-1",
            ticker="AAPL",
            published_at="2026-04-26T01:00:00Z",
            score=0.5,
        )
        session.commit()

        blocks = compute_sentiment_percentile(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T03:00:00Z",
            sentiment_min_observations=SENTIMENT_MIN_OBSERVATIONS,
        )
        per_ticker = blocks[0].payload["per_ticker"]
        assert per_ticker["AAPL"]["percentile"] == pytest.approx(50.0, abs=0.1)


# ---------------------------------------------------------------------------
# Bootstrap path — too few baseline observations
# ---------------------------------------------------------------------------


class TestSentimentPercentileBootstrap:
    def test_bootstrap_fires_when_per_ticker_baseline_below_minimum(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        # AAPL has only 10 observations — below the 30 min
        _add_baseline(
            session,
            ticker="AAPL",
            as_of="2026-04-26T03:00:00Z",
            mean=0.0,
            stdev=0.1,
            n_observations=10,
            state=CalibrationState.BOOTSTRAP,
        )
        # MSFT acts as the universe-pool source: calibrated with 50 obs
        _add_baseline(
            session,
            ticker="MSFT",
            as_of="2026-04-26T03:00:00Z",
            mean=0.05,
            stdev=0.2,
            n_observations=50,
            state=CalibrationState.CALIBRATED,
        )
        _add_recent_article(
            session,
            article_id="art-aapl",
            ticker="AAPL",
            published_at="2026-04-26T01:00:00Z",
            score=0.3,
        )
        session.commit()

        blocks = compute_sentiment_percentile(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T03:00:00Z",
            sentiment_min_observations=SENTIMENT_MIN_OBSERVATIONS,
        )
        assert len(blocks) == 1
        block = blocks[0]
        # Block-level state escalates to BOOTSTRAP because at least one
        # per-ticker entry is bootstrap-tagged.
        assert block.calibration_state is CalibrationState.BOOTSTRAP
        entry = block.payload["per_ticker"]["AAPL"]
        assert entry["calibration_state"] == "bootstrap"
        # Baseline used should be the universe pool (mean=0.05, stdev=0.2)
        assert entry["baseline_mean"] == pytest.approx(0.05)
        assert entry["baseline_stdev"] == pytest.approx(0.2)
        assert "bootstrap_reason" in entry

    def test_bootstrap_when_no_per_ticker_baseline_row_exists(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        _add_ticker(session, "MSFT")
        # No AAPL baseline; MSFT calibrated to act as pool
        _add_baseline(
            session,
            ticker="MSFT",
            as_of="2026-04-26T03:00:00Z",
            mean=0.05,
            stdev=0.2,
            n_observations=50,
            state=CalibrationState.CALIBRATED,
        )
        _add_recent_article(
            session,
            article_id="art-aapl",
            ticker="AAPL",
            published_at="2026-04-26T01:00:00Z",
            score=0.3,
        )
        session.commit()

        blocks = compute_sentiment_percentile(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T03:00:00Z",
            sentiment_min_observations=SENTIMENT_MIN_OBSERVATIONS,
        )
        assert len(blocks) == 1
        entry = blocks[0].payload["per_ticker"]["AAPL"]
        assert entry["calibration_state"] == "bootstrap"


# ---------------------------------------------------------------------------
# Per-ticker percentile reflects per-name distribution
# ---------------------------------------------------------------------------


class TestSentimentPercentileReflectsPerTickerDistribution:
    def test_same_current_value_yields_different_percentiles_for_different_baselines(
        self, session: Session
    ) -> None:
        """Per-ticker calibration: TSLA's stdev wider than JPM's → same current
        reading lands at different percentile per name."""
        _add_ticker(session, "TSLA", sector="tech")
        _add_ticker(session, "JPM", sector="financials")
        # TSLA: wide range → 0.2 is well within distribution
        _add_baseline(
            session,
            ticker="TSLA",
            as_of="2026-04-25T00:00:00Z",
            mean=0.0,
            stdev=0.5,
            n_observations=50,
        )
        # JPM: narrow range → 0.2 is way out
        _add_baseline(
            session,
            ticker="JPM",
            as_of="2026-04-25T00:00:00Z",
            mean=0.0,
            stdev=0.05,
            n_observations=50,
        )
        # Same current reading 0.2 for both
        _add_recent_article(
            session,
            article_id="art-tsla",
            ticker="TSLA",
            published_at="2026-04-26T01:00:00Z",
            score=0.2,
        )
        _add_recent_article(
            session,
            article_id="art-jpm",
            ticker="JPM",
            published_at="2026-04-26T01:00:00Z",
            score=0.2,
        )
        session.commit()

        blocks = compute_sentiment_percentile(
            session,
            ticker_scope=("TSLA", "JPM"),
            as_of="2026-04-26T03:00:00Z",
            sentiment_min_observations=SENTIMENT_MIN_OBSERVATIONS,
        )
        # Two blocks — one per sector audience
        by_audience = {next(iter(b.audience)): b for b in blocks}
        tsla_pct = by_audience[OutputAudience.SECTOR_TECH_SEMIS].payload["per_ticker"]["TSLA"][
            "percentile"
        ]
        jpm_pct = by_audience[OutputAudience.SECTOR_FINANCIALS].payload["per_ticker"]["JPM"][
            "percentile"
        ]
        # TSLA at 0.2/0.5 stdev → ~65.5 percentile
        # JPM at 0.2/0.05 stdev → ~99.99+ percentile
        assert tsla_pct < 80.0
        assert jpm_pct > 99.0
        assert jpm_pct > tsla_pct
