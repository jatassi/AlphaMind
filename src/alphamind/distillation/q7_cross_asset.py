"""Q7 cross-asset and correlation computations — story 02-distillation-layer/08d.

Authoritative scope: ``docs/design/02-distillation-layer/external.md``
§ 2 From cross-asset and correlation (quant 7). The deterministic
computations cover intra-sector pairwise correlation matrices and divergence
detection, cross-sector rotation classification with narrative tagging,
breadth and market internals, intermarket regime signals, lead-lag
relationships with overdue and inversion flags, and correlation regime change
detection with the narrative-lag indicator.

The implementation is split across the :mod:`alphamind.distillation.q7`
sub-package so each indicator family lives in its own module. This file is
the import shim the orchestrator (story 12) and tests reach for: it
re-exports the public API from each sub-module so call sites do not need
to know the internal layout.

Structure:

- :mod:`alphamind.distillation.q7._helpers` — shared math primitives.
- :mod:`alphamind.distillation.q7.intra_sector_correlation` — intra-sector
  correlation matrices and divergence detection.
- :mod:`alphamind.distillation.q7.cross_sector_rotation` — cross-sector
  rotation classification with narrative tagging.
- :mod:`alphamind.distillation.q7.breadth_internals` — breadth and market
  internals.
- :mod:`alphamind.distillation.q7.intermarket_regime` — intermarket regime
  signals.
- :mod:`alphamind.distillation.q7.lead_lag` — lead-lag relationships with
  overdue and inversion flags.
- :mod:`alphamind.distillation.q7.correlation_regime_change` — correlation
  breakdown, dispersion shift, and narrative-lag detection.
- :mod:`alphamind.distillation.q7.assemble` — per-block-type assemblers and
  the top-level :func:`assemble_q7_blocks` entry point.
"""

from __future__ import annotations

from alphamind.distillation.q7 import (
    EMA_WINDOWS_DAYS,
    GLD_TICKER,
    NARRATIVE_LAG_REGIME_TAGS,
    OIL_SERIES,
    OIL_SOURCE,
    REAL_YIELD_SERIES,
    REAL_YIELD_SOURCE,
    ROTATION_NARRATIVE_GROWTH_DRIVEN,
    ROTATION_NARRATIVE_RATE_DRIVEN,
    ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN,
    SPY_TICKER,
    TLT_TICKER,
    VELOCITY_SHARP,
    VELOCITY_SLOW,
    VIX_SERIES,
    VIX_SOURCE,
    XLE_TICKER,
    CorrelationRegimeChangeConfig,
    LeadLagPair,
    assemble_q7_blocks,
    compute_breadth_internals,
    compute_correlation_regime_change,
    compute_cross_sector_rotation,
    compute_intermarket_regime,
    compute_intra_sector_correlation,
    compute_lead_lag,
    compute_pair_correlations,
)

__all__ = [
    "EMA_WINDOWS_DAYS",
    "GLD_TICKER",
    "NARRATIVE_LAG_REGIME_TAGS",
    "OIL_SERIES",
    "OIL_SOURCE",
    "REAL_YIELD_SERIES",
    "REAL_YIELD_SOURCE",
    "ROTATION_NARRATIVE_GROWTH_DRIVEN",
    "ROTATION_NARRATIVE_RATE_DRIVEN",
    "ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN",
    "SPY_TICKER",
    "TLT_TICKER",
    "VELOCITY_SHARP",
    "VELOCITY_SLOW",
    "VIX_SERIES",
    "VIX_SOURCE",
    "XLE_TICKER",
    "CorrelationRegimeChangeConfig",
    "LeadLagPair",
    "assemble_q7_blocks",
    "compute_breadth_internals",
    "compute_correlation_regime_change",
    "compute_cross_sector_rotation",
    "compute_intermarket_regime",
    "compute_intra_sector_correlation",
    "compute_lead_lag",
    "compute_pair_correlations",
]
