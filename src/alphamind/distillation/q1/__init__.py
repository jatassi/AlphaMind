"""Q1 price/volume indicator and anomaly computations — story 02-distillation/08a.

The package implements the deterministic price/volume computations from
``docs/design/02-distillation-layer/external.md`` § 2 From price and volume
(quant 1) plus the price/volume anomaly detections from
``docs/design/02-distillation-layer/external.md`` § 3 Anomaly detection.

Submodules group by indicator family rather than per-bar computation so the
public surface exposed by :mod:`alphamind.distillation.q1_price_volume`
stays small.

The orchestrator (story 12) calls :func:`assemble_q1_blocks` to produce
every Q1 :class:`OutputBlock` for an invocation; the function fans out per
``(sector_audience, indicator_group)`` and returns a flat list.
"""

from alphamind.distillation.q1.assemble import (
    BLOCK_ID_PRICE_MOVE_ANOMALY,
    BLOCK_ID_VOLUME_ANOMALY,
    assemble_q1_blocks,
)

__all__ = [
    "BLOCK_ID_PRICE_MOVE_ANOMALY",
    "BLOCK_ID_VOLUME_ANOMALY",
    "assemble_q1_blocks",
]
