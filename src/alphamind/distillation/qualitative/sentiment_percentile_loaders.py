"""IO shell for the per-ticker sentiment percentile classifier (ALP-487).

Owns every DB read the sentiment-percentile compute needs. The pure
compute lives in :mod:`.sentiment_percentile_compute`. The reads project
through the pilot-scoped
:class:`alphamind.distillation._repository.DistillationRepository`
Protocol; no raw ``Session`` use here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from alphamind.distillation._repository import (
    DistillationRepository,
    TickerBaselineRow,
)
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.qualitative._sector_audience import sector_audience_map
from alphamind.distillation.qualitative.sentiment_percentile_compute import (
    SENTIMENT_PROXY_WINDOW_HOURS,
    SentimentPercentileInputs,
)


def _parse_iso_utc(ts: str) -> datetime:
    if ts.endswith("Z"):
        return datetime.fromisoformat(ts[:-1]).replace(tzinfo=UTC)
    return datetime.fromisoformat(ts)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_start_hours(*, as_of: str, hours: int) -> str:
    end = _parse_iso_utc(as_of)
    return _format_iso_utc(end - timedelta(hours=hours))


def load_sentiment_percentile_inputs(
    repository: DistillationRepository,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    sector_audience_by_ticker: Mapping[str, OutputAudience] | None = None,
) -> SentimentPercentileInputs:
    """Pre-load every per-ticker input the sentiment-percentile compute consumes.

    ``sector_audience_by_ticker`` may be passed by the whole-category loader
    so the same map is shared across qualitative sub-modules; when ``None``
    it is resolved here against the repository.
    """
    scope = tuple(ticker_scope)
    audience_map = (
        dict(sector_audience_by_ticker)
        if sector_audience_by_ticker is not None
        else sector_audience_map(repository, ticker_scope=scope)
    )
    if not scope:
        return SentimentPercentileInputs(
            ticker_scope=(),
            freshness_ts=_parse_iso_utc(as_of),
            sector_audience_by_ticker={},
            current_sentiment_by_ticker={},
            baseline_by_ticker={},
            universe_pooled_sentiment=None,
        )

    proxy_range_start = _window_start_hours(as_of=as_of, hours=SENTIMENT_PROXY_WINDOW_HOURS)
    current_sentiment_by_ticker: dict[str, tuple[float, int] | None] = {}
    baseline_by_ticker: dict[str, TickerBaselineRow | None] = {}
    for ticker in scope:
        if ticker not in audience_map:
            continue
        current_sentiment_by_ticker[ticker] = repository.load_news_article_sentiment_scores(
            ticker=ticker, range_start=proxy_range_start, range_end=as_of
        )
        baseline_by_ticker[ticker] = repository.load_latest_baseline(
            ticker=ticker, kind="sentiment", as_of=as_of
        )
    universe_pooled = repository.load_universe_pooled_sentiment_distribution(as_of=as_of)
    return SentimentPercentileInputs(
        ticker_scope=scope,
        freshness_ts=_parse_iso_utc(as_of),
        sector_audience_by_ticker=audience_map,
        current_sentiment_by_ticker=current_sentiment_by_ticker,
        baseline_by_ticker=baseline_by_ticker,
        universe_pooled_sentiment=universe_pooled,
    )


__all__ = ["load_sentiment_percentile_inputs"]
