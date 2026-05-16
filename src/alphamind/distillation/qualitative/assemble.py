"""Qualitative top-level assembly entry point (ALP-487).

Two entry points live here:

* :func:`assemble_qualitative_blocks_from_inputs` — pure compute over a
  frozen :class:`QualitativeInputs` (see :mod:`._loaders`). The
  orchestrator's Phase 2 calls this under
  ``asyncio.TaskGroup`` + ``asyncio.to_thread``.
* :func:`assemble_qualitative_blocks` — thin session-accepting shim that
  wraps the session in a :class:`SqlDistillationRepository`, pre-loads the
  :class:`QualitativeInputs`, and delegates to the pure compute.

Pure-compute composition: the assembly path concatenates each
sub-classifier's compute output into one block list. The three
classifiers do not share state at compute time — every cross-classifier
piece (sector audience map, contract scope) is pre-loaded once by the
whole-category loader and threaded into the per-classifier inputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.output import OutputBlock
from alphamind.distillation.qualitative._loaders import (
    QualitativeInputs,
    load_qualitative_inputs,
)
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
    access. This is the function the orchestrator's Phase 2 calls under
    ``asyncio.TaskGroup`` + ``asyncio.to_thread``.
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


def assemble_qualitative_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    ticker_scope: Sequence[str],
    contract_scope: Sequence[str],
    as_of: datetime,
) -> list[OutputBlock]:
    """Session-accepting shim that delegates to the pure assembly path.

    Constructs a :class:`SqlDistillationRepository`, pre-loads the
    :class:`QualitativeInputs`, and delegates to
    :func:`assemble_qualitative_blocks_from_inputs`.
    """
    repository = SqlDistillationRepository(session)
    inputs = load_qualitative_inputs(
        repository,
        config=config,
        ticker_scope=ticker_scope,
        contract_scope=contract_scope,
        as_of=as_of,
    )
    return assemble_qualitative_blocks_from_inputs(inputs, config=config)


__all__ = [
    "assemble_qualitative_blocks",
    "assemble_qualitative_blocks_from_inputs",
]
