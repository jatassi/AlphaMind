"""news_search on-demand tool — ALP-247.

Queries ``news_articles`` joined to ``news_article_tickers``, ranks results
by a recency + tier + ticker-mention composite, and returns up to
``max_results`` typed articles.

The ranking constants are named so that story 04a's input-bundle assembler
can import them rather than re-defining its own set.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind._kernel.clock import Clock, RealClock
from alphamind.analysis.news_freshness import NewsEmptyReason, diagnose_empty_news
from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, format_iso, parse_iso
from alphamind.persistence.models import NewsArticles, NewsArticleTickers

__all__ = [
    # Exported constants for story 04a to import.
    "NEWS_TICKER_MENTION_BOOST",
    "NEWS_TIER_SCORE",
    "NewsSearchArticle",
    "NewsSearchInput",
    "NewsSearchOutput",
    "news_search_factory",
]

# ---------------------------------------------------------------------------
# Ranking constants (shared with story 04a)
# ---------------------------------------------------------------------------

NEWS_TIER_SCORE: dict[str, float] = {"tier_1": 3.0, "tier_2": 2.0, "tier_3": 1.0}
_TIER_DEFAULT = "tier_3"
_TIER_VALUES: frozenset[str] = frozenset(NEWS_TIER_SCORE)

NEWS_TICKER_MENTION_BOOST = 1.5

_BODY_EXCERPT_CHARS = 500


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class NewsSearchInput(BaseModel, frozen=True):
    """Input for the news_search tool.

    At least one of ``query`` or ``tickers`` must be non-empty.
    Empty-both is handled at call time by returning UNAVAILABLE rather than raising.
    """

    query: str = ""
    tickers: tuple[str, ...] = ()
    lookback_hours: int = Field(default=24, ge=1)
    max_results: int = Field(default=20, ge=1)


class NewsSearchArticle(BaseModel, frozen=True):
    headline: str
    source: str
    source_outlet: str | None
    source_credibility_tier: str | None
    published_at: datetime
    url: str | None
    summary: str | None
    tickers: tuple[str, ...]
    topic_tags: tuple[str, ...]
    relevance_score: float
    body_excerpt: str | None


NewsSearchReason = Literal["invalid_input", "vendor_api_error", "no_data"]


class NewsSearchOutput(ToolEnvelope, frozen=True):
    """Tool output.

    ``reason`` disambiguates the three unavailable states for the LLM caller
    (ALP-567): ``"invalid_input"`` when the payload had no query and no
    tickers, ``"vendor_api_error"`` when the collector has not ingested any
    news within the lookback window (collapses the diagnostic's
    ``NO_ROWS_IN_DB`` and ``COLLECTOR_INACTIVE`` causes into one operator-
    visible code), and ``"no_data"`` when the collector is current but the
    specific query/ticker filter has no matching rows. ``None`` when the
    search returned at least one article (``quality == COMPLETE``).
    """

    articles: tuple[NewsSearchArticle, ...]
    reason: NewsSearchReason | None = None


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _parse_topic_tags(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(tag) for tag in parsed if isinstance(tag, str))


def _score_news_article(
    *,
    tier: str,
    published_at: datetime,
    as_of: datetime,
    lookback_hours: int,
    ticker_match: bool,
) -> float:
    """Recency + tier composite; optionally boosted by ticker-mention match.

    Named constant references allow story 04a to import instead of re-define.
    """
    tier_score = NEWS_TIER_SCORE.get(tier, NEWS_TIER_SCORE[_TIER_DEFAULT])
    age_hours = max((as_of - published_at).total_seconds() / 3600.0, 0.0)
    recency = max(1.0 - (age_hours / lookback_hours), 0.0)
    base = tier_score + recency
    return base * NEWS_TICKER_MENTION_BOOST if ticker_match else base


def _read_body_excerpt(body_path: str | None) -> str | None:
    """Read the first ``_BODY_EXCERPT_CHARS`` characters from the body file."""
    if body_path is None:
        return None
    try:
        text = Path(body_path).read_text(encoding="utf-8", errors="replace")
        return text[:_BODY_EXCERPT_CHARS] if text else None
    except OSError:
        return None


_REASON_BY_EMPTY_DIAGNOSIS: dict[NewsEmptyReason, NewsSearchReason] = {
    NewsEmptyReason.NO_ROWS_IN_DB: "vendor_api_error",
    NewsEmptyReason.COLLECTOR_INACTIVE: "vendor_api_error",
    NewsEmptyReason.NO_HEADLINES_IN_WINDOW: "no_data",
}


def _search_news(session: Session, inp: NewsSearchInput, clock: Clock) -> NewsSearchOutput:
    now = clock.now()

    if not inp.query and not inp.tickers:
        return NewsSearchOutput(
            articles=(),
            data_freshness=now,
            quality=ToolQuality.UNAVAILABLE,
            reason="invalid_input",
        )

    as_of = now
    window_start = as_of - timedelta(hours=inp.lookback_hours)
    window_start_iso = format_iso(window_start)
    as_of_iso = format_iso(as_of)

    # Push both filters into SQL: a JOIN/IN against ``news_article_tickers``
    # constrains by ticker, and a ``LIKE`` on ``lower(headline_text)``
    # constrains by query. ``DISTINCT`` deduplicates the ticker join.
    article_stmt = (
        select(
            NewsArticles.article_id,
            NewsArticles.headline_text,
            NewsArticles.source,
            NewsArticles.source_outlet,
            NewsArticles.source_credibility_tier,
            NewsArticles.published_at,
            NewsArticles.url,
            NewsArticles.topic_tags,
            NewsArticles.body_path,
        )
        .where(
            NewsArticles.published_at >= window_start_iso,
            NewsArticles.published_at <= as_of_iso,
        )
        .distinct()
    )
    if inp.query:
        # Tokenize on whitespace and require each token to appear in the article
        # (AND semantics). Each token is matched against ``headline_text`` OR
        # ``topic_tags`` (OR within a token) so that catalyst keywords stored as
        # tags surface alongside headline matches. This makes multi-word catalyst
        # phrases (e.g. "IBM earnings guidance") match articles whose headlines
        # contain the tokens non-contiguously — fixing the whole-phrase miss that
        # left adaptive-researcher queries returning no_data (ALP-777).
        # SQLite's ``lower()`` is ASCII-only; acceptable for the English corpus.
        for tok in inp.query.split():
            escaped = tok.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            article_stmt = article_stmt.where(
                func.lower(NewsArticles.headline_text).like(f"%{escaped}%", escape="\\")
                | func.lower(NewsArticles.topic_tags).like(f"%{escaped}%", escape="\\")
            )
    if inp.tickers:
        ticker_uppers = tuple(t.upper() for t in inp.tickers)
        article_stmt = article_stmt.join(
            NewsArticleTickers,
            NewsArticleTickers.article_id == NewsArticles.article_id,
        ).where(NewsArticleTickers.ticker.in_(ticker_uppers))

    article_rows = session.execute(article_stmt).all()

    if not article_rows:
        diagnosis = diagnose_empty_news(session, window_start=window_start)
        return NewsSearchOutput(
            articles=(),
            data_freshness=as_of,
            quality=ToolQuality.UNAVAILABLE,
            reason=_REASON_BY_EMPTY_DIAGNOSIS[diagnosis.reason],
        )

    article_ids = [row.article_id for row in article_rows]
    ticker_rows = session.execute(
        select(NewsArticleTickers.article_id, NewsArticleTickers.ticker).where(
            NewsArticleTickers.article_id.in_(article_ids)
        )
    ).all()
    tickers_by_article: dict[str, list[str]] = {}
    for art_id, ticker in ticker_rows:
        tickers_by_article.setdefault(art_id, []).append(ticker)

    ticker_set = frozenset(t.upper() for t in inp.tickers)

    scored: list[tuple[float, datetime, NewsSearchArticle]] = []
    for row in article_rows:
        article_tickers = tuple(tickers_by_article.get(row.article_id, ()))
        published_at = parse_iso(row.published_at)
        raw_tier = row.source_credibility_tier
        tier = raw_tier if raw_tier in _TIER_VALUES else _TIER_DEFAULT
        ticker_match = bool(ticker_set and any(t.upper() in ticker_set for t in article_tickers))
        score = _score_news_article(
            tier=tier,
            published_at=published_at,
            as_of=as_of,
            lookback_hours=inp.lookback_hours,
            ticker_match=ticker_match,
        )
        article = NewsSearchArticle(
            headline=row.headline_text or "",
            source=row.source or "",
            source_outlet=row.source_outlet,
            source_credibility_tier=cast(str | None, raw_tier),
            published_at=published_at,
            url=row.url,
            summary=None,
            tickers=article_tickers,
            topic_tags=_parse_topic_tags(row.topic_tags),
            relevance_score=score,
            body_excerpt=_read_body_excerpt(row.body_path),
        )
        scored.append((score, published_at, article))

    scored.sort(key=lambda item: (-item[0], -item[1].timestamp()))
    articles = tuple(art for _, _, art in scored[: inp.max_results])

    freshness_row = session.execute(
        select(NewsArticles.ingested_at).order_by(NewsArticles.ingested_at.desc()).limit(1)
    ).scalar()
    freshness = parse_iso(freshness_row) if freshness_row else as_of

    if not articles:
        # Reachable only when max_results clamps the result set to zero — every
        # matched row already produced a scored article above.
        return NewsSearchOutput(
            articles=(),
            data_freshness=freshness,
            quality=ToolQuality.UNAVAILABLE,
            reason="no_data",
        )
    return NewsSearchOutput(
        articles=articles,
        data_freshness=freshness,
        quality=ToolQuality.COMPLETE,
    )


def news_search_factory(
    session: Session, *, clock: Clock | None = None
) -> Callable[[NewsSearchInput], NewsSearchOutput]:
    """Return a callable suitable for the Claude Agent SDK tool registry.

    ``clock`` defaults to :class:`RealClock`; tests pass a fake to control
    the timestamp deterministically (ALP-474).
    """
    resolved_clock: Clock = clock if clock is not None else RealClock()

    def _call(inp: NewsSearchInput) -> NewsSearchOutput:
        return _search_news(session, inp, resolved_clock)

    return _call
