"""
Finnhub news collector — Qual1.

Collects company-specific news (/company-news) and market-wide general news
(/news?category=general).  Writes ``news_articles`` and
``news_article_tickers``.  Forward-only (no bootstrap).
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import finnhub
import requests.exceptions
import yaml
from finnhub.exceptions import FinnhubAPIException

from alphamind.data_sources._common import (
    RateLimiter,
    RetryShape,
    RunState,
    active_universe_tickers,
    resume_since,
    track_run,
    with_retries,
)
from alphamind.persistence.models import Base, NewsArticles, NewsArticleTickers
from alphamind.persistence.session import make_engine, make_session_factory

# Exceptions surfaced by ``_fetch_company_news`` / ``_fetch_general_news`` after
# ``with_retries`` exhausts ``vendor_outage_extended`` — these are exactly the
# transient classes ``_is_retryable`` would have approved, so catching them
# isolates the failing endpoint without masking programming bugs.
_TRANSIENT_FETCH_ERRORS = (FinnhubAPIException, requests.exceptions.RequestException)

logger = logging.getLogger(__name__)

_PROVIDER = "finnhub"
_DEFAULT_LOOKBACK_HOURS = 24


def _get_api_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "")


def _news_dir() -> Path:
    env = os.environ.get("ALPHAMIND_NEWS_DIR")
    if env:
        return Path(env)
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(userprofile) / "AlphaMind" / "data" / "news"


def _load_outlets() -> dict[str, str]:
    """Load news_outlets.yaml once per call; returns {outlet_name: tier}."""
    config_path = Path(__file__).parents[4] / "config" / "news_outlets.yaml"
    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {name: entry["tier"] for name, entry in data.get("outlets", {}).items()}


def _article_id(url: str, published_at: str) -> str:
    return hashlib.sha256(f"{_PROVIDER}\x00{url}\x00{published_at}".encode()).hexdigest()


def _persist_body(article_id: str, published_at_iso: str, body: str) -> str:
    """Write body text to disk; return the file path string."""
    dt = datetime.fromisoformat(published_at_iso)
    dest = _news_dir() / str(dt.year) / f"{dt.month:02d}" / f"{article_id}.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    return str(dest)


def _upsert_article(
    sess: Any,
    article_id: str,
    headline: str,
    source_outlet: str | None,
    url: str,
    published_at: str,
    body: str,
    outlets: dict[str, str],
) -> bool:
    """Insert a news_articles row; return True if inserted, False if duplicate."""
    if sess.get(NewsArticles, article_id) is not None:
        return False

    body_path: str | None = None
    if body:
        body_path = _persist_body(article_id, published_at, body)

    tier = outlets.get(source_outlet) if source_outlet else None
    now = datetime.now(UTC).isoformat()
    sess.add(
        NewsArticles(
            article_id=article_id,
            source=_PROVIDER,
            source_outlet=source_outlet,
            source_credibility_tier=tier,
            url=url,
            language="en",
            headline_text=headline,
            body_path=body_path,
            published_at=published_at,
            ingested_at=now,
            vendor_sentiment_score=None,
            vendor_sentiment_label=None,
        )
    )
    return True


def _upsert_ticker_link(sess: Any, article_id: str, ticker: str) -> None:
    if sess.get(NewsArticleTickers, (article_id, ticker)) is None:
        sess.add(NewsArticleTickers(article_id=article_id, ticker=ticker, is_primary=1))


# ``vendor_outage_extended`` (4 attempts, 5+15+45s = 65s budget): finnhub's
# 01:30 / 06:30 ET cycle boundaries hit a multi-second 502 nginx window every
# day; the prior ``important`` 1s budget exhausted inside the blip and the
# whole cycle died. The same blip hits ``/news?category=general``, so both
# endpoints use the longer schedule. See ALP-419 for the recurring symptom.
@with_retries(RetryShape.vendor_outage_extended)
def _fetch_company_news(
    sdk: finnhub.Client, ticker: str, from_date: str, to_date: str
) -> list[dict[str, Any]]:
    return sdk.company_news(ticker, _from=from_date, to=to_date) or []


@with_retries(RetryShape.vendor_outage_extended)
def _fetch_general_news(sdk: finnhub.Client) -> list[dict[str, Any]]:
    return sdk.general_news("general") or []


def _gather_items(
    sdk: finnhub.Client,
    ticker_scope: list[str],
    from_date: str,
    to_date: str,
    rate_limiter: RateLimiter | None,
    run: RunState,
) -> list[tuple[dict[str, Any], str | None]]:
    """Fetch company news per ticker and market-wide general news.

    A persistent fetch failure on one target (``vendor_outage_extended``
    retries exhausted on a 502, a delisted symbol, a per-ticker rate-limit
    hit) is logged at WARNING and the target is skipped — earlier targets'
    items still persist and the cycle continues. Failed targets surface on
    ``run.error_summary`` so ``collection_runs`` records the degraded outcome.
    """
    items: list[tuple[dict[str, Any], str | None]] = []
    failed_targets: list[str] = []
    for ticker in ticker_scope:
        if rate_limiter:
            rate_limiter.acquire(_PROVIDER)
        try:
            ticker_items = _fetch_company_news(sdk, ticker, from_date, to_date)
        except _TRANSIENT_FETCH_ERRORS:
            logger.warning("finnhub.news: skipping %s after fetch failure", ticker, exc_info=True)
            failed_targets.append(ticker)
            continue
        for item in ticker_items:
            items.append((item, ticker))
    if rate_limiter:
        rate_limiter.acquire(_PROVIDER)
    try:
        general_items = _fetch_general_news(sdk)
    except _TRANSIENT_FETCH_ERRORS:
        logger.warning("finnhub.news: skipping general feed after fetch failure", exc_info=True)
        failed_targets.append("general")
    else:
        for item in general_items:
            items.append((item, None))
    if failed_targets:
        run.error_summary = (
            f"skipped {len(failed_targets)} targets after fetch failure: {','.join(failed_targets)}"
        )
    return items


def _write_items(
    items: list[tuple[dict[str, Any], str | None]], sf: Any, outlets: dict[str, str]
) -> int:
    """Persist fetched items and return count of newly inserted rows."""
    rows_written = 0
    with sf() as sess:
        for item, primary_ticker in items:
            url = item.get("url") or ""
            published_unix = item.get("datetime") or 0
            published_at = datetime.fromtimestamp(published_unix, tz=UTC).isoformat()
            art_id = _article_id(url, published_at)
            headline = item.get("headline") or ""
            body = item.get("summary") or ""
            outlet = item.get("source")
            inserted = _upsert_article(
                sess, art_id, headline, outlet, url, published_at, body, outlets
            )
            if inserted and primary_ticker:
                _upsert_ticker_link(sess, art_id, primary_ticker)
            if inserted:
                rows_written += 1
        sess.commit()
    return rows_written


def collect_news(
    ticker_scope: list[str] | None = None,
    since: datetime | None = None,
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
    _rate_limiter: RateLimiter | None = None,
    _sdk: Any = None,
) -> None:
    """
    Pull company news for each ticker in ``ticker_scope`` and market-wide
    general news.  Writes ``news_articles`` and ``news_article_tickers``.

    Parameters
    ----------
    ticker_scope:
        List of tickers to request company-specific news for.  Defaults to
        active universe tickers (benchmarks excluded).
    since:
        Earliest published_at to request.  Defaults to the latest stored
        Finnhub article minus a 1-hour overlap, or 24 hours ago when the
        table is empty.
    _sdk:
        Optional Finnhub SDK override (anything implementing ``FinnhubSDK``).
        Defaults to a freshly-constructed ``finnhub.Client``.
    """
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    if ticker_scope is None:
        ticker_scope = active_universe_tickers(include_benchmarks=False, session_factory=sf)
    if since is None:
        since = resume_since(
            column=NewsArticles.published_at,
            filters=(NewsArticles.source == "finnhub",),
            default_lookback=timedelta(hours=_DEFAULT_LOOKBACK_HOURS),
            overlap=timedelta(hours=1),
            session_factory=sf,
        )
    if _rate_limiter is None:
        # Finnhub free tier returns 429 well below the documented 60/min when
        # bursting; pin to 30/min to guarantee >=2s spacing between calls.
        _rate_limiter = RateLimiter()
        _rate_limiter.set_limit(_PROVIDER, rate_per_minute=30)

    sdk = _sdk if _sdk is not None else finnhub.Client(api_key=_get_api_key())
    outlets = _load_outlets()
    now = datetime.now(UTC)
    from_date = since.strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    with track_run("finnhub.news", _repo=_repo) as run:
        items = _gather_items(sdk, ticker_scope, from_date, to_date, _rate_limiter, run)
        run.rows_written = _write_items(items, sf, outlets)
