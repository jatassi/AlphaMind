"""Tests for news_search tool — ALP-247.

Uses an in-memory SQLite database for full integration coverage.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis.tools import TOOLS, ToolQuality
from alphamind.analysis.tools.news_search import NewsSearchInput, NewsSearchOutput
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    NewsArticles,
    NewsArticleTickers,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


_RECENT = datetime.now(UTC) - timedelta(hours=1)


def _add_ticker(session: Session, ticker: str) -> None:
    asset_id = f"asset-{ticker.lower()}"
    session.add(
        AssetUniverse(
            asset_id=asset_id,
            ticker=ticker,
            full_name=f"{ticker} Corp",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2026-01-01",
            last_updated="2026-01-01T00:00:00Z",
        )
    )


def _add_article(
    session: Session,
    *,
    article_id: str,
    headline: str,
    published_at: datetime,
    tier: str = "tier_1",
    tickers: tuple[str, ...] = (),
    ingested_at: datetime | None = None,
) -> None:
    ing = ingested_at or published_at
    session.add(
        NewsArticles(
            article_id=article_id,
            source="test",
            source_outlet="TestOutlet",
            source_credibility_tier=tier,
            url=None,
            language="en",
            headline_text=headline,
            body_path=None,
            published_at=published_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ingested_at=ing.strftime("%Y-%m-%dT%H:%M:%SZ"),
            topic_tags=None,
        )
    )
    session.flush()  # flush article before adding tickers (FK: article_id)
    for t in tickers:
        session.add(
            NewsArticleTickers(
                article_id=article_id,
                ticker=t,
                is_primary=1,
            )
        )


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


def test_news_search_by_ticker_returns_matching_articles(session: Session) -> None:
    _add_ticker(session, "NVDA")
    _add_ticker(session, "AAPL")
    session.flush()
    _add_article(
        session,
        article_id="art-1",
        headline="NVDA earnings beat",
        published_at=_RECENT,
        tickers=("NVDA",),
    )
    _add_article(
        session,
        article_id="art-2",
        headline="AAPL supply chain news",
        published_at=_RECENT,
        tickers=("AAPL",),
    )
    session.commit()

    tool = TOOLS["news_search"]
    fn = tool.callable_factory(session)
    result: NewsSearchOutput = fn(NewsSearchInput(tickers=("NVDA",)))

    assert isinstance(result, NewsSearchOutput)
    assert len(result.articles) == 1
    assert result.articles[0].headline == "NVDA earnings beat"
    assert "NVDA" in result.articles[0].tickers


def test_news_search_output_carries_envelope_fields(session: Session) -> None:
    _add_ticker(session, "NVDA")
    session.flush()
    _add_article(
        session,
        article_id="art-1",
        headline="NVDA beats estimates",
        published_at=_RECENT,
        tickers=("NVDA",),
    )
    session.commit()

    fn = TOOLS["news_search"].callable_factory(session)
    result: NewsSearchOutput = fn(NewsSearchInput(tickers=("NVDA",)))

    assert isinstance(result.data_freshness, datetime)
    assert isinstance(result.quality, ToolQuality)


def test_news_search_by_query_filters_headline(session: Session) -> None:
    _add_ticker(session, "AAPL")
    session.flush()
    _add_article(
        session,
        article_id="art-1",
        headline="FOMC raises rates again",
        published_at=_RECENT,
        tickers=("AAPL",),
    )
    _add_article(
        session,
        article_id="art-2",
        headline="AAPL guidance cut",
        published_at=_RECENT,
        tickers=("AAPL",),
    )
    session.commit()

    fn = TOOLS["news_search"].callable_factory(session)
    result: NewsSearchOutput = fn(NewsSearchInput(query="FOMC"))

    assert len(result.articles) == 1
    assert "FOMC" in result.articles[0].headline


def test_news_search_quality_complete_when_results_found(session: Session) -> None:
    _add_ticker(session, "TSLA")
    session.flush()
    _add_article(
        session,
        article_id="art-1",
        headline="TSLA delivery miss",
        published_at=_RECENT,
        tickers=("TSLA",),
    )
    session.commit()

    fn = TOOLS["news_search"].callable_factory(session)
    result: NewsSearchOutput = fn(NewsSearchInput(tickers=("TSLA",)))

    assert result.quality == ToolQuality.COMPLETE


# ---------------------------------------------------------------------------
# Tests: invalid inputs / missing data
# ---------------------------------------------------------------------------


def test_news_search_empty_query_and_tickers_returns_unavailable(session: Session) -> None:
    """Empty query AND empty tickers must not raise — returns UNAVAILABLE envelope."""
    session.commit()

    fn = TOOLS["news_search"].callable_factory(session)
    result: NewsSearchOutput = fn(NewsSearchInput())

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.articles == ()
    assert isinstance(result.data_freshness, datetime)


def test_news_search_no_matching_articles_returns_unavailable(session: Session) -> None:
    """Ticker with no articles → UNAVAILABLE."""
    _add_ticker(session, "MSFT")
    session.commit()

    fn = TOOLS["news_search"].callable_factory(session)
    result: NewsSearchOutput = fn(NewsSearchInput(tickers=("MSFT",)))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.articles == ()
