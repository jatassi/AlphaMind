"""Whole-category IO shell for qualitative-derived (ALP-487).

Composes the per-classifier loaders into a single :class:`QualitativeInputs`
the pure compute (:func:`.assemble.assemble_qualitative_blocks_from_inputs`)
consumes. The composition root in
:mod:`alphamind.distillation.orchestrator` calls
:func:`load_qualitative_inputs` sequentially under the shared SQLAlchemy
session (this is the IO shell half) and then dispatches the pure compute
inside an ``asyncio.to_thread`` call inside the Phase 2 ``TaskGroup``.

The sector → audience map is resolved once here and threaded into the
per-ticker sub-loaders so the news/price divergence and sentiment-percentile
classifiers share the result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository import DistillationRepository
from alphamind.distillation.qualitative._sector_audience import sector_audience_map
from alphamind.distillation.qualitative.news_price_divergence_compute import (
    NewsPriceDivergenceInputs,
)
from alphamind.distillation.qualitative.news_price_divergence_loaders import (
    load_news_price_divergence_inputs,
)
from alphamind.distillation.qualitative.prediction_market_deltas_compute import (
    PredictionMarketDeltasInputs,
)
from alphamind.distillation.qualitative.prediction_market_deltas_loaders import (
    load_prediction_market_deltas_inputs,
)
from alphamind.distillation.qualitative.sentiment_percentile_compute import (
    SentimentPercentileInputs,
)
from alphamind.distillation.qualitative.sentiment_percentile_loaders import (
    load_sentiment_percentile_inputs,
)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class QualitativeInputs:
    """Frozen pre-loaded inputs for the qualitative compute path.

    Bundles the three per-classifier input dataclasses plus the resolved
    scope tuples. The pure compute reads only this object — no DB access
    is needed once the loader returns.
    """

    ticker_scope: tuple[str, ...]
    contract_scope: tuple[str, ...]
    as_of_iso: str
    news_price_divergence: NewsPriceDivergenceInputs
    sentiment_percentile: SentimentPercentileInputs
    prediction_market_deltas: PredictionMarketDeltasInputs


def load_qualitative_inputs(
    repository: DistillationRepository,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    contract_scope: Sequence[str],
    as_of: datetime,
) -> QualitativeInputs:
    """Pre-load every qualitative input under the shared session.

    Three things happen in order:

    1. The sector → audience map is resolved once and threaded into the
       news/price divergence and sentiment-percentile sub-loaders so they
       share the result instead of re-querying ``sector_classification``.
    2. Each sub-loader runs sequentially, pre-loading the per-ticker (or
       per-contract) rows its compute consumes.
    3. The composed :class:`QualitativeInputs` is returned for the pure
       compute path.
    """
    as_of_iso = _format_iso_utc(as_of)
    ticker_scope_t = tuple(ticker_scope)
    contract_scope_t = tuple(contract_scope)
    audience_map = sector_audience_map(repository, ticker_scope=ticker_scope_t)

    news_price_inputs = load_news_price_divergence_inputs(
        repository,
        ticker_scope=ticker_scope_t,
        as_of=as_of_iso,
        window_hours=config.anomaly_detection.news_price_divergence_window_hours,
        sector_audience_by_ticker=audience_map,
    )
    sentiment_inputs = load_sentiment_percentile_inputs(
        repository,
        ticker_scope=ticker_scope_t,
        as_of=as_of_iso,
        sector_audience_by_ticker=audience_map,
    )
    prediction_market_inputs = load_prediction_market_deltas_inputs(
        repository,
        contract_scope=contract_scope_t,
        as_of=as_of_iso,
        history_days=config.persistence_windows.prediction_market_history_days,
    )

    return QualitativeInputs(
        ticker_scope=ticker_scope_t,
        contract_scope=contract_scope_t,
        as_of_iso=as_of_iso,
        news_price_divergence=news_price_inputs,
        sentiment_percentile=sentiment_inputs,
        prediction_market_deltas=prediction_market_inputs,
    )


__all__ = ["QualitativeInputs", "load_qualitative_inputs"]
