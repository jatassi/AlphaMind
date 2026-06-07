"""Qualitative top-level pure-compute assembly entry point.

:func:`assemble_qualitative_blocks_from_inputs` operates over a frozen
:class:`QualitativeInputs` (see :mod:`._loaders`); the per-category
indicator compute step calls it under ``asyncio.TaskGroup`` +
``asyncio.to_thread``. The
three classifiers do not share state at compute time — every
cross-classifier piece (sector audience map, contract scope) is
pre-loaded once by the whole-category loader and threaded into the
per-classifier inputs.
"""

from __future__ import annotations

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.output import OutputBlock
from alphamind.distillation.qualitative._loaders import QualitativeInputs
from alphamind.distillation.qualitative.news_price_divergence_compute import (
    compute_news_price_divergence_blocks,
)
from alphamind.distillation.qualitative.prediction_market_deltas_compute import (
    compute_prediction_market_delta_blocks,
)
from alphamind.distillation.qualitative.sentiment_percentile_compute import (
    compute_sentiment_percentile_blocks,
)


def assemble_qualitative_blocks_from_inputs(
    inputs: QualitativeInputs,
    *,
    config: DistillationDomainConfig,
) -> list[OutputBlock]:
    """Pure-compute assembly of every qualitative :class:`OutputBlock`.

    Operates entirely on the pre-loaded :class:`QualitativeInputs`; no DB
    access.
    """
    blocks: list[OutputBlock] = []
    blocks.extend(
        compute_news_price_divergence_blocks(
            inputs.news_price_divergence,
            min_articles=config.anomaly_detection.news_price_divergence_min_articles,
        )
    )
    blocks.extend(
        compute_sentiment_percentile_blocks(
            inputs.sentiment_percentile,
            sentiment_min_observations=config.persistence_windows.sentiment_min_observations,
        )
    )
    blocks.extend(
        compute_prediction_market_delta_blocks(
            inputs.prediction_market_deltas,
            delta_pp_threshold=config.prediction_market.prediction_market_delta_pp_threshold,
            low_liquidity_volume_min_usd=(
                config.prediction_market.prediction_market_low_liquidity_volume_min_usd
            ),
        )
    )
    return blocks


__all__ = ["assemble_qualitative_blocks_from_inputs"]
