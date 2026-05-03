"""Tests for news_search tool — ALP-247.

Uses an in-memory SQLite database for full integration coverage.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
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


# ---------------------------------------------------------------------------
# Tests: SQL-side filter pushdown (perf — PR #11 /review item 4)
# ---------------------------------------------------------------------------


def _spy_news_articles_scan_sizes(engine: Engine, scan_sizes: list[int]) -> Callable[..., None]:
    """Attach a SQLAlchemy event listener that re-runs each ``news_articles``
    statement against the underlying DBAPI to count its row count.

    Mirrors the pattern in ``test_refresh_ticker_baselines.py`` —
    ``cursor.rowcount`` is unreliable for SQLite SELECTs, so we re-execute
    the same SQL with the same parameters to count rows. Returns the
    listener function so the caller can detach it via ``event.remove``.
    """

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "from news_articles" not in statement.lower():
            return
        cur = conn.connection.cursor()
        try:
            cur.execute(statement, parameters or ())
            scan_sizes.append(len(cur.fetchall()))
        finally:
            cur.close()

    return _after_cursor_execute


def test_news_search_ticker_filter_pushed_down_to_sql(engine: Engine, session: Session) -> None:
    """Ticker filter must restrict rows at the database level, not in Python.

    Seeds 100 articles where only 10 mention NVDA. Captures the row count
    of every ``news_articles`` statement and asserts that none returns the
    full 100-row set — proving the JOIN/WHERE constrained the result before
    it crossed the Python boundary.
    """
    _add_ticker(session, "NVDA")
    _add_ticker(session, "AAPL")
    session.flush()

    nvda_count = 10
    other_count = 90
    for i in range(nvda_count):
        _add_article(
            session,
            article_id=f"nvda-{i}",
            headline=f"NVDA story {i}",
            published_at=_RECENT,
            tickers=("NVDA",),
        )
    for i in range(other_count):
        _add_article(
            session,
            article_id=f"aapl-{i}",
            headline=f"AAPL story {i}",
            published_at=_RECENT,
            tickers=("AAPL",),
        )
    session.commit()

    scan_sizes: list[int] = []
    listener = _spy_news_articles_scan_sizes(engine, scan_sizes)
    try:
        fn = TOOLS["news_search"].callable_factory(session)
        result: NewsSearchOutput = fn(NewsSearchInput(tickers=("NVDA",), max_results=100))
    finally:
        event.remove(engine, "after_cursor_execute", listener)

    assert len(result.articles) == nvda_count
    for article in result.articles:
        assert "NVDA" in article.headline
        assert "NVDA" in article.tickers

    assert scan_sizes, "expected at least one news_articles query"
    assert max(scan_sizes) <= nvda_count, (
        f"news_articles query returned {max(scan_sizes)} rows; "
        f"ticker filter was not pushed down to SQL (expected ≤ {nvda_count})"
    )


def test_news_search_query_filter_pushed_down_to_sql(engine: Engine, session: Session) -> None:
    """Headline ``query`` filter must restrict rows at the database level."""
    _add_ticker(session, "AAPL")
    session.flush()

    fomc_count = 5
    other_count = 95
    for i in range(fomc_count):
        _add_article(
            session,
            article_id=f"fomc-{i}",
            headline=f"FOMC raises rates {i}",
            published_at=_RECENT,
            tickers=("AAPL",),
        )
    for i in range(other_count):
        _add_article(
            session,
            article_id=f"misc-{i}",
            headline=f"AAPL misc news {i}",
            published_at=_RECENT,
            tickers=("AAPL",),
        )
    session.commit()

    scan_sizes: list[int] = []
    listener = _spy_news_articles_scan_sizes(engine, scan_sizes)
    try:
        fn = TOOLS["news_search"].callable_factory(session)
        result: NewsSearchOutput = fn(NewsSearchInput(query="FOMC", max_results=100))
    finally:
        event.remove(engine, "after_cursor_execute", listener)

    assert len(result.articles) == fomc_count
    for article in result.articles:
        assert "FOMC" in article.headline

    assert scan_sizes, "expected at least one news_articles query"
    assert max(scan_sizes) <= fomc_count, (
        f"news_articles query returned {max(scan_sizes)} rows; "
        f"query filter was not pushed down to SQL (expected ≤ {fomc_count})"
    )
