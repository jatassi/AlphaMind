"""Shared locus-block construction for q7 correlation paths (ALP-632).

The cross-universe sigma test (``correlation_regime_change_compute``) and
the per-sector divergence detector (``intra_sector_correlation_compute``)
both produce streams of pair-level breakdown signals that often express
one underlying single-name dislocation as N independent-looking flags.
Both paths roll those streams up into per-ticker
``q7.correlation_locus.<ticker>`` blocks consumed by the same brief
renderer; this module owns the shared payload contract so the two
producers stay in lock-step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import _BLOCK_NAMESPACE


@dataclass(frozen=True, slots=True)
class CorrelationLocusContribution:
    """One pair contributing to a locus rollup, with its deviation magnitude.

    Each producer constructs these from its own native pair representation
    (the cross-universe path from ``_PublishedPair``, the per-sector path
    from the divergence ``AnomalyFlag`` stream); the shared builder reads
    only the three fields below.
    """

    row: str
    col: str
    magnitude: float


def build_correlation_locus_block(
    *,
    locus_ticker: str,
    contributions: Sequence[CorrelationLocusContribution],
    sector_by_ticker: Mapping[str, str] | None,
    supporting_pair_ids: Sequence[str],
    as_of: datetime,
) -> OutputBlock:
    """Construct one ``q7.correlation_locus.<ticker>`` block.

    Payload contract (consumed by
    :func:`alphamind.distillation.correlation_brief._decompose_correlation_locus_block`):

    - ``locus_ticker`` — the name the rollup is centred on.
    - ``pair_count`` — number of contributing pairs.
    - ``max_deviation_sigma`` — strongest contributing deviation magnitude
      in the *producer's native sigma unit* (cross-universe path: Fisher-z
      deviation divided by the null-distribution stdev; per-sector path:
      raw-correlation deviation divided by the sector's long-window
      off-diagonal pstdev). Both producers report on a sigma scale but the
      scales are not directly comparable across paths — the brief renders
      both opaquely as ``sigma`` and downstream consumers treat the value
      as a within-producer severity ranking, not a cross-producer absolute.
    - ``partner_tickers`` — sorted partner-ticker list (excluding the locus).
    - ``supporting_pairs`` — producer-native references (block ids for the
      cross-universe path, flag names for the per-sector path) so a
      consumer can map back to the originating per-pair signal.
    - ``partners_by_sector`` / ``cross_sector_spread`` — optional sector
      grouping. ``None`` when no sector context is supplied.
    """
    partners = sorted(
        {
            contribution.col if contribution.row == locus_ticker else contribution.row
            for contribution in contributions
        }
    )
    max_sigma = max(contribution.magnitude for contribution in contributions)
    partners_by_sector: dict[str, list[str]] | None = None
    cross_sector_spread: str | None = None
    if sector_by_ticker is not None:
        partners_by_sector = {}
        for partner in partners:
            sector = sector_by_ticker.get(partner, "unknown")
            partners_by_sector.setdefault(sector, []).append(partner)
        if len(partners_by_sector) == 1:
            (only_sector,) = partners_by_sector.keys()
            cross_sector_spread = f"{only_sector}-only"
        else:
            cross_sector_spread = f"{len(partners_by_sector)} sectors"
    payload: dict[str, object] = {
        "locus_ticker": locus_ticker,
        "pair_count": len(contributions),
        "max_deviation_sigma": max_sigma,
        "partner_tickers": partners,
        "supporting_pairs": tuple(supporting_pair_ids),
        "partners_by_sector": partners_by_sector,
        "cross_sector_spread": cross_sector_spread,
    }
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.correlation_locus.{locus_ticker}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload=payload,
        anomaly_flags=(
            AnomalyFlag(
                name=f"correlation_locus_flag:{locus_ticker}",
                magnitude=max_sigma,
                severity="investigate_now",
            ),
        ),
        regime_context=None,
    )


__all__ = [
    "CorrelationLocusContribution",
    "build_correlation_locus_block",
]
