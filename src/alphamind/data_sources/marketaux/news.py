"""
Marketaux news collector — Qual1 vendor adapter.

Pulls /v1/news/all per-ticker and market-wide, writes:
- news_articles (with body_path on disk, vendor_sentiment_score/label)
- news_article_tickers
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from alphamind.data_sources._common import (
    active_universe_tickers,
    default_session_factory,
    resume_since,
    track_run,
)
from alphamind.data_sources.marketaux.client import MarketauxClient
from alphamind.persistence.models import NewsArticles, NewsArticleTickers

_TICKER_BATCH_SIZE = 5  # Marketaux free-tier max symbols per request

_UNSET = object()  # sentinel for "caller did not supply ticker_scope"

# Sentiment thresholds per story spec
_SENTIMENT_POS_THRESHOLD = 0.15
_SENTIMENT_NEG_THRESHOLD = -0.15


def _load_outlets() -> dict[str, str]:
    """Load news_outlets.yaml and return {outlet_name: tier} mapping."""
    config_dir = Path(__file__).parents[4] / "config"
    path = config_dir / "news_outlets.yaml"
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    return {name: entry["tier"] for name, entry in data.get("outlets", {}).items()}


def _make_article_id(url: str, published_at: str) -> str:
    raw = f"marketaux|{url}|{published_at}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _derive_sentiment_label(score: float | None) -> str | None:
    if score is None:
        return None
    if score > _SENTIMENT_POS_THRESHOLD:
        return "positive"
    if score < _SENTIMENT_NEG_THRESHOLD:
        return "negative"
    return "neutral"


def _default_body_dir() -> str:
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return str(Path(userprofile) / "AlphaMind" / "data" / "news")


def _write_body(body_dir: str, article_id: str, published_at: str, text: str) -> str:
    """Write body text to disk and return the file path."""
    try:
        dt = datetime.fromisoformat(published_at)
    except (ValueError, AttributeError):
        dt = datetime.now(UTC)

    year = dt.strftime("%Y")
    month = dt.strftime("%m")
    dir_path = Path(body_dir) / year / month
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / f"{article_id}.txt"
    file_path.write_text(text, encoding="utf-8")
    return str(file_path)


def _known_tickers(session_factory: Any) -> frozenset[str]:
    """Return the set of tickers present in asset_universe."""
    from sqlalchemy import text

    with session_factory() as sess:
        rows = sess.execute(text("SELECT ticker FROM asset_universe")).fetchall()
    return frozenset(r[0] for r in rows)


def _ingest_articles(
    articles: list[dict[str, Any]],
    query_ticker: str | None,
    outlets: dict[str, str],
    body_dir: str,
    session_factory: Any,
    universe: frozenset[str],
) -> int:
    """Persist articles and return count of rows written."""
    ingested_at = datetime.now(UTC).isoformat()
    rows_written = 0

    for art in articles:
        url = art.get("url", "")
        published_at = art.get("published_at", "")
        article_id = _make_article_id(url, published_at)

        entities: list[dict[str, Any]] = art.get("entities", [])
        primary_entity = next(
            (e for e in entities if e.get("symbol") == query_ticker),
            entities[0] if entities else None,
        )
        sentiment_score: float | None = (
            primary_entity.get("sentiment_score") if primary_entity else None
        )
        sentiment_label = _derive_sentiment_label(sentiment_score)

        source_outlet = art.get("source")
        credibility_tier = outlets.get(source_outlet) if source_outlet else None

        topics: list[dict[str, Any]] = art.get("topics", [])
        names = [t.get("name") for t in topics if t.get("name")]
        topic_tags = json.dumps(names) if names else None

        description = art.get("description") or ""
        body_path: str | None = None
        if description:
            body_path = _write_body(body_dir, article_id, published_at, description)

        article_row = NewsArticles(
            article_id=article_id,
            source="marketaux",
            source_outlet=source_outlet,
            source_credibility_tier=credibility_tier,
            url=url,
            language=art.get("language", "en"),
            headline_text=art.get("title", ""),
            body_path=body_path,
            published_at=published_at,
            ingested_at=ingested_at,
            vendor_sentiment_score=sentiment_score,
            vendor_sentiment_label=sentiment_label,
            topic_tags=topic_tags,
        )

        ticker_rows = [
            NewsArticleTickers(
                article_id=article_id,
                ticker=sym,
                is_primary=1 if sym == query_ticker else 0,
            )
            for entity in entities
            if (sym := entity.get("symbol")) and sym in universe
        ]

        with session_factory() as sess:
            if sess.get(NewsArticles, article_id) is not None:
                continue
            sess.add(article_row)
            for tr in ticker_rows:
                sess.merge(tr)
            sess.commit()

        rows_written += 1

    return rows_written


def collect_news(
    ticker_scope: list[str] | None = _UNSET,  # type: ignore[assignment]
    since: str | None = None,
    *,
    _client: MarketauxClient | None = None,
    _session_factory: Any = None,
    _repo: Any = None,
    _body_dir: str | None = None,
) -> None:
    """
    Pull Marketaux news and persist to news_articles + news_article_tickers.

    Parameters
    ----------
    ticker_scope:
        List of tickers to query (batched in groups of 5).  When omitted,
        defaults to active universe tickers (benchmarks excluded).  Pass
        ``None`` explicitly for a market-wide US query only.
    since:
        ISO 8601 datetime string; only articles published after this are
        fetched.  Defaults to the latest stored Marketaux article minus a
        1-hour overlap, or 24 hours ago when the table is empty.
    _client:
        Injectable MarketauxClient (for testing).
    _session_factory:
        Injectable SQLAlchemy session factory (for testing).
    _repo:
        Injectable track_run repository (for testing).
    _body_dir:
        Injectable body-text base directory (for testing).
    """
    if _client is None:
        import os

        from alphamind.data_sources.marketaux.client import MarketauxClient

        api_key = os.environ.get("MARKETAUX_API_KEY")
        if not api_key:
            raise RuntimeError("MARKETAUX_API_KEY is not set in the environment")
        _client = MarketauxClient(api_key=api_key)

    if _session_factory is None:
        _session_factory = default_session_factory()

    if ticker_scope is _UNSET:
        ticker_scope = active_universe_tickers(
            include_benchmarks=False, session_factory=_session_factory
        )

    if since is None:
        _since_dt = resume_since(
            column=NewsArticles.published_at,
            filters=(NewsArticles.source == "marketaux",),
            default_lookback=timedelta(hours=24),
            overlap=timedelta(hours=1),
            session_factory=_session_factory,
        )
        # Marketaux rejects ISO timestamps with sub-second precision; truncate.
        since = _since_dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")

    body_dir = _body_dir if _body_dir is not None else _default_body_dir()
    outlets = _load_outlets()

    with track_run("marketaux.news", _repo=_repo) as run:
        universe = _known_tickers(_session_factory)
        total_written = 0

        if ticker_scope:
            for i in range(0, len(ticker_scope), _TICKER_BATCH_SIZE):
                batch = ticker_scope[i : i + _TICKER_BATCH_SIZE]
                articles = _client.get_news(symbols=batch, published_after=since)
                # First ticker in batch drives the primary-flag assignment
                total_written += _ingest_articles(
                    articles, batch[0], outlets, body_dir, _session_factory, universe
                )
        else:
            articles = _client.get_news(symbols=None, countries="us", published_after=since)
            total_written += _ingest_articles(
                articles, None, outlets, body_dir, _session_factory, universe
            )

        run.rows_written = total_written
