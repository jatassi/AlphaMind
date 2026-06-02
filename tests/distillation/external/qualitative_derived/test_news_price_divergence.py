"""Tests for news-price divergence detection — story 02-distillation-layer/08f.

Cover ``compute_news_price_divergence``: per-ticker aggregation of
``vendor_sentiment_label`` over the trailing window, dominant-direction
gating at the 60% non-neutral cutoff, cross-reference with hourly OHLCV
price action, and ``priced_in`` / ``hidden_problem`` flag emission per
``docs/design/02-distillation-layer/external.md`` § 2 News-price divergence.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.distillation.qualitative_derived import (
    compute_news_price_divergence,
)
from alphamind.persistence.models import (
    AssetUniverse,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    SectorClassification,
)

# Mirror config/distillation.yaml; tests scope to scenarios where the
# orchestrator (story 12) has resolved them, so the test file is the right
# place to anchor the fixtures.
NEWS_PRICE_WINDOW_HOURS = 12
NEWS_PRICE_MIN_ARTICLES = 5


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


def _add_news(
    session: Session,
    *,
    article_id: str,
    ticker: str,
    published_at: str,
    label: str,
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
            vendor_sentiment_score=None,
            vendor_sentiment_label=label,
        )
    )


def _add_hour_bar(
    session: Session,
    *,
    ticker: str,
    period_start: str,
    open_: float,
    close: float,
) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1h",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=open_,
            adj_high=max(open_, close),
            adj_low=min(open_, close),
            adj_close=close,
            adj_volume=10_000,
            adj_vwap=None,
            unadj_open=open_,
            unadj_high=max(open_, close),
            unadj_low=min(open_, close),
            unadj_close=close,
            unadj_volume=10_000,
            unadj_vwap=None,
            trade_count=None,
            source="test",
            ingested_at=period_start,
        )
    )


# ---------------------------------------------------------------------------
# Happy path — priced_in (dominant negative + price rising)
# ---------------------------------------------------------------------------


class TestNewsPriceDivergencePricedIn:
    def test_dominant_negative_news_with_rising_price_emits_priced_in(
        self, session: Session
    ) -> None:
        _add_ticker(session, "AAPL")
        # 8 negative + 2 positive = 80% negative non-neutral → above 60% cutoff
        for i in range(8):
            _add_news(
                session,
                article_id=f"art-neg-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:00:00Z",
                label="negative",
            )
        for i in range(2):
            _add_news(
                session,
                article_id=f"art-pos-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{16 + i:02d}:00:00Z",
                label="positive",
            )
        # Price rises across the window: open=100, close=105 → +5
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=101.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=104.0,
            close=105.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        assert len(blocks) == 1
        block = blocks[0]
        assert isinstance(block, OutputBlock)
        assert block.block_id == "qual.news_price_divergence"
        assert OutputAudience.SECTOR_TECH_SEMIS in block.audience
        assert block.calibration_state is CalibrationState.CALIBRATED
        assert "AAPL" in block.payload["per_ticker"]
        per_ticker = block.payload["per_ticker"]["AAPL"]
        assert per_ticker["direction"] == "priced_in"
        assert per_ticker["dominant_label"] == "negative"
        assert len(block.anomaly_flags) == 1
        flag = block.anomaly_flags[0]
        assert flag.name == "news_price_divergence"
        assert flag.severity == "investigate_now"

    def test_dominant_positive_news_with_falling_price_emits_hidden_problem(
        self, session: Session
    ) -> None:
        _add_ticker(session, "AAPL")
        # 7 positive + 2 negative + 1 neutral. 7/9 non-neutral = 77.7% > 60%
        for i in range(7):
            _add_news(
                session,
                article_id=f"art-pos-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:00:00Z",
                label="positive",
            )
        for i in range(2):
            _add_news(
                session,
                article_id=f"art-neg-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{16 + i:02d}:00:00Z",
                label="negative",
            )
        _add_news(
            session,
            article_id="art-neu-0",
            ticker=Symbol("AAPL"),
            published_at="2026-04-25T18:00:00Z",
            label="neutral",
        )
        # Price falls: open=100, close=95
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=99.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=96.0,
            close=95.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        assert len(blocks) == 1
        per_ticker = blocks[0].payload["per_ticker"]["AAPL"]
        assert per_ticker["direction"] == "hidden_problem"
        assert per_ticker["dominant_label"] == "positive"

    def test_dominant_negative_news_with_flat_price_emits_priced_in(self, session: Session) -> None:
        """A halt-resume open or illiquid name producing exact-zero price change.

        Per the docstring, the boundary of "flat or rising" includes
        exact-zero. The classifier must not silently drop the divergence
        on the corner case.
        """
        _add_ticker(session, "AAPL")
        for i in range(8):
            _add_news(
                session,
                article_id=f"art-neg-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:00:00Z",
                label="negative",
            )
        for i in range(2):
            _add_news(
                session,
                article_id=f"art-pos-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{16 + i:02d}:00:00Z",
                label="positive",
            )
        # First-bar open and last-bar close are identical → price_change == 0.0
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=101.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=99.0,
            close=100.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        assert len(blocks) == 1
        per_ticker = blocks[0].payload["per_ticker"]["AAPL"]
        assert per_ticker["direction"] == "priced_in"


# ---------------------------------------------------------------------------
# Negative path — agreement (no divergence)
# ---------------------------------------------------------------------------


class TestNewsPriceDivergenceAgreement:
    def test_negative_news_with_falling_price_does_not_fire(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        for i in range(8):
            _add_news(
                session,
                article_id=f"art-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:00:00Z",
                label="negative",
            )
        # Price falls — agreement, no divergence
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=99.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=96.0,
            close=95.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        # No divergence → no block emitted for this ticker
        assert blocks == []

    def test_positive_news_with_rising_price_does_not_fire(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        for i in range(8):
            _add_news(
                session,
                article_id=f"art-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:00:00Z",
                label="positive",
            )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=101.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=104.0,
            close=105.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        assert blocks == []


# ---------------------------------------------------------------------------
# Calibration gating — bootstrap when evidence is thin
# ---------------------------------------------------------------------------


class TestNewsPriceDivergenceCalibrationGating:
    def test_thin_evidence_emits_bootstrap_tagged_block(self, session: Session) -> None:
        """Two negative articles + rising price still emit, but tagged BOOTSTRAP.

        The dominant direction is set by a thin sample; per
        ``threshold-calibration.md`` § Bootstrap policy the layer
        produces with tag rather than aborting, so the divergence block
        emits with ``calibration_state == BOOTSTRAP`` and a reason
        string naming the article-count gap. Downstream consumers
        weight the conviction accordingly.
        """
        _add_ticker(session, "AAPL")
        # Two negative articles (below NEWS_PRICE_MIN_ARTICLES = 5).
        for i in range(2):
            _add_news(
                session,
                article_id=f"art-neg-{i}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + i:02d}:00:00Z",
                label="negative",
            )
        # Rising price → divergence direction is priced_in.
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=101.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=104.0,
            close=105.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        assert len(blocks) == 1
        block = blocks[0]
        assert block.calibration_state is CalibrationState.ACCUMULATING
        assert block.bootstrap_reason is not None
        assert "news_price_divergence_min_articles" in block.bootstrap_reason
        assert "2 < 5" in block.bootstrap_reason
        # The block payload still surfaces the divergence so downstream
        # consumers can read the thin signal — the calibration tag
        # carries the caveat.
        per_ticker = block.payload["per_ticker"]["AAPL"]
        assert per_ticker["direction"] == "priced_in"


# ---------------------------------------------------------------------------
# Dominant-direction threshold: 60% gate
# ---------------------------------------------------------------------------


class TestDominantDirectionThreshold:
    def test_at_61_percent_non_neutral_dominant_label_is_assigned(self, session: Session) -> None:
        _add_ticker(session, "AAPL")
        # 61 negative + 39 positive of 100 non-neutral = 61% negative
        for i in range(61):
            _add_news(
                session,
                article_id=f"art-neg-{i:03d}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:{i % 60:02d}:00Z",
                label="negative",
            )
        for i in range(39):
            _add_news(
                session,
                article_id=f"art-pos-{i:03d}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:{i % 60:02d}:30Z",
                label="positive",
            )
        # Rising price → divergence with dominant negative
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=101.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=104.0,
            close=105.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        assert len(blocks) == 1
        per_ticker = blocks[0].payload["per_ticker"]["AAPL"]
        assert per_ticker["direction"] == "priced_in"
        assert per_ticker["dominant_label"] == "negative"

    def test_at_59_percent_non_neutral_no_dominant_label_is_assigned(
        self, session: Session
    ) -> None:
        _add_ticker(session, "AAPL")
        # 59 negative + 41 positive of 100 non-neutral = 59% negative — below cutoff
        for i in range(59):
            _add_news(
                session,
                article_id=f"art-neg-{i:03d}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:{i % 60:02d}:00Z",
                label="negative",
            )
        for i in range(41):
            _add_news(
                session,
                article_id=f"art-pos-{i:03d}",
                ticker=Symbol("AAPL"),
                published_at=f"2026-04-25T{14 + (i % 6):02d}:{i % 60:02d}:30Z",
                label="positive",
            )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T14:00:00Z",
            open_=100.0,
            close=101.0,
        )
        _add_hour_bar(
            session,
            ticker=Symbol("AAPL"),
            period_start="2026-04-25T22:00:00Z",
            open_=104.0,
            close=105.0,
        )
        session.commit()

        blocks = compute_news_price_divergence(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-26T02:00:00Z",
            window_hours=NEWS_PRICE_WINDOW_HOURS,
            min_articles=NEWS_PRICE_MIN_ARTICLES,
        )
        # No dominant direction → no divergence block
        assert blocks == []
