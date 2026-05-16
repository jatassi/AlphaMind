"""IO shell for the news/price divergence classifier (ALP-487).

Owns every DB read the divergence detection needs. The pure compute lives
in :mod:`.news_price_divergence_compute`. The repository-shaped reads
project through the pilot-scoped
:class:`alphamind.distillation._repository.DistillationRepository`
Protocol; no raw ``Session`` use here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from alphamind.distillation._repository import (
    DistillationRepository,
    NewsLabelCountsRow,
)
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.qualitative._sector_audience import sector_audience_map
from alphamind.distillation.qualitative.news_price_divergence_compute import (
    NewsPriceDivergenceInputs,
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


def load_news_price_divergence_inputs(
    repository: DistillationRepository,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    window_hours: int,
    sector_audience_by_ticker: Mapping[str, OutputAudience] | None = None,
) -> NewsPriceDivergenceInputs:
    """Pre-load every per-ticker input the divergence compute consumes.

    ``sector_audience_by_ticker`` may be passed in by the whole-category
    loader so the same map is shared across qualitative sub-modules; when
    ``None`` it is resolved here against the repository.
    """
    scope = tuple(ticker_scope)
    audience_map = (
        dict(sector_audience_by_ticker)
        if sector_audience_by_ticker is not None
        else sector_audience_map(repository, ticker_scope=scope)
    )
    if not scope:
        return NewsPriceDivergenceInputs(
            ticker_scope=(),
            freshness_ts=_parse_iso_utc(as_of),
            sector_audience_by_ticker={},
            label_counts_by_ticker={},
            price_change_by_ticker={},
        )

    range_start = _window_start_hours(as_of=as_of, hours=window_hours)
    label_counts_by_ticker: dict[str, NewsLabelCountsRow] = {}
    price_change_by_ticker: dict[str, float | None] = {}
    for ticker in scope:
        if ticker not in audience_map:
            continue
        label_counts_by_ticker[ticker] = repository.load_news_article_label_counts(
            ticker=ticker, range_start=range_start, range_end=as_of
        )
        price_change_by_ticker[ticker] = repository.load_hourly_window_price_change(
            ticker=ticker, range_start=range_start, range_end=as_of
        )
    return NewsPriceDivergenceInputs(
        ticker_scope=scope,
        freshness_ts=_parse_iso_utc(as_of),
        sector_audience_by_ticker=audience_map,
        label_counts_by_ticker=label_counts_by_ticker,
        price_change_by_ticker=price_change_by_ticker,
    )


__all__ = ["load_news_price_divergence_inputs"]
