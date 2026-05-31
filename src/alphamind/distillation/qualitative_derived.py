"""Backward-compat shim for the qualitative-derived classifiers.

ALP-487 decomposed the legacy single-file module into the
:mod:`alphamind.distillation.qualitative` package
(per-classifier ``*_compute.py`` + ``*_loaders.py`` pairs). This file
preserves the legacy session-accepting public API so existing callers
(orchestrator imports, ``tests/distillation/external/qualitative_derived/``
suite) continue to work without import churn.

The three exported functions each:

1. Construct a :class:`SqlDistillationRepository` from the session.
2. Pre-load the per-classifier inputs through the IO shell.
3. Delegate to the pure compute function on those inputs.

No additional logic lives here — the routing is mechanical. New work
should target the per-classifier ``compute_*_blocks`` entry points
directly with pre-loaded inputs.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.output import OutputBlock
from alphamind.distillation.qualitative.news_price_divergence_compute import (
    NON_NEUTRAL_DOMINANCE_THRESHOLD,
    compute_news_price_divergence_blocks,
)
from alphamind.distillation.qualitative.news_price_divergence_loaders import (
    load_news_price_divergence_inputs,
)
from alphamind.distillation.qualitative.prediction_market_deltas_compute import (
    compute_prediction_market_delta_blocks,
)
from alphamind.distillation.qualitative.prediction_market_deltas_loaders import (
    load_prediction_market_deltas_inputs,
)
from alphamind.distillation.qualitative.sentiment_percentile_compute import (
    SENTIMENT_PROXY_WINDOW_HOURS,
    compute_sentiment_percentile_blocks,
)
from alphamind.distillation.qualitative.sentiment_percentile_loaders import (
    load_sentiment_percentile_inputs,
)


def compute_news_price_divergence(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    window_hours: int,
    min_articles: int,
) -> list[OutputBlock]:
    """Session-accepting shim — pre-loads inputs and delegates to compute."""
    repository = SqlDistillationRepository(session)
    inputs = load_news_price_divergence_inputs(
        repository,
        ticker_scope=ticker_scope,
        as_of=as_of,
        window_hours=window_hours,
    )
    return compute_news_price_divergence_blocks(inputs, min_articles=min_articles)


def compute_sentiment_percentile(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    sentiment_min_observations: int,
) -> list[OutputBlock]:
    """Session-accepting shim — pre-loads inputs and delegates to compute."""
    repository = SqlDistillationRepository(session)
    inputs = load_sentiment_percentile_inputs(
        repository,
        ticker_scope=ticker_scope,
        as_of=as_of,
    )
    return compute_sentiment_percentile_blocks(
        inputs, sentiment_min_observations=sentiment_min_observations
    )


def compute_prediction_market_deltas(
    session: Session,
    *,
    contract_scope: Sequence[str],
    as_of: str,
    delta_pp_threshold: float,
    low_liquidity_volume_min_usd: float,
    prediction_market_history_days: int,
) -> list[OutputBlock]:
    """Session-accepting shim — pre-loads inputs and delegates to compute."""
    repository = SqlDistillationRepository(session)
    inputs = load_prediction_market_deltas_inputs(
        repository,
        contract_scope=contract_scope,
        as_of=as_of,
        history_days=prediction_market_history_days,
    )
    return compute_prediction_market_delta_blocks(
        inputs,
        delta_pp_threshold=delta_pp_threshold,
        low_liquidity_volume_min_usd=low_liquidity_volume_min_usd,
    )


__all__ = [
    "NON_NEUTRAL_DOMINANCE_THRESHOLD",
    "SENTIMENT_PROXY_WINDOW_HOURS",
    "compute_news_price_divergence",
    "compute_prediction_market_deltas",
    "compute_sentiment_percentile",
]
