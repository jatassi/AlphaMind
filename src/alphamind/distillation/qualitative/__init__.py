"""Qualitative-derived deterministic computations.

This package decomposes the legacy single-file
:mod:`alphamind.distillation.qualitative_derived` into per-classifier
``*_compute.py`` + ``*_loaders.py`` pairs plus a whole-category shell. The
three classifiers covered (per
``docs/design/02-distillation-layer/external.md`` § 2 From qualitative data):

* news/price divergence — :mod:`.news_price_divergence_compute`
* sentiment percentile — :mod:`.sentiment_percentile_compute`
* prediction-market deltas — :mod:`.prediction_market_deltas_compute`

The split is the qualitative half of the propagation tracked under ALP-467
(the q1 pilot). After this story, qualitative becomes pure compute and
lifts into its own ``asyncio.TaskGroup`` task in Phase 2 of the
orchestrator — see :func:`alphamind.distillation.orchestrator._run_phase_2`.

The session-accepting public surface that legacy callers (and the
``tests/distillation/external/qualitative_derived/`` test suite) consume
is re-exported from :mod:`alphamind.distillation.qualitative_derived`,
which is now a thin backward-compat shim over the per-classifier modules.
"""

from __future__ import annotations

from alphamind.distillation.qualitative._loaders import (
    QualitativeInputs,
    load_qualitative_inputs,
)
from alphamind.distillation.qualitative.assemble import (
    assemble_qualitative_blocks_from_inputs,
)
from alphamind.distillation.qualitative.news_price_divergence_compute import (
    NON_NEUTRAL_DOMINANCE_THRESHOLD,
    compute_news_price_divergence_blocks,
)
from alphamind.distillation.qualitative.prediction_market_deltas_compute import (
    compute_prediction_market_delta_blocks,
)
from alphamind.distillation.qualitative.sentiment_percentile_compute import (
    SENTIMENT_PROXY_WINDOW_HOURS,
    compute_sentiment_percentile_blocks,
)

__all__ = [
    "NON_NEUTRAL_DOMINANCE_THRESHOLD",
    "SENTIMENT_PROXY_WINDOW_HOURS",
    "QualitativeInputs",
    "assemble_qualitative_blocks_from_inputs",
    "compute_news_price_divergence_blocks",
    "compute_prediction_market_delta_blocks",
    "compute_sentiment_percentile_blocks",
    "load_qualitative_inputs",
]
