"""Q7 cross-asset and correlation computations — story 02-distillation-layer/08d.

The implementation is split across this sub-package so each concern lives in
its own module. The top-level entry point
:mod:`alphamind.distillation.q7_cross_asset` re-exports the public API from
each sub-module so call sites do not need to know the internal layout.

Structure:

- :mod:`alphamind.distillation.q7._helpers` — shared math primitives
  (Pearson correlation, log returns, z-score, window queries).
- :mod:`alphamind.distillation.q7.intra_sector_correlation` — intra-sector
  pairwise correlation matrices and divergence detection.
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

from alphamind.distillation.q7.assemble import (
    assemble_q7_blocks,
    compute_pair_correlations,
)
from alphamind.distillation.q7.breadth_internals import (
    EMA_WINDOWS_DAYS,
    compute_breadth_internals,
)
from alphamind.distillation.q7.correlation_regime_change import (
    NARRATIVE_LAG_REGIME_TAGS,
    CorrelationRegimeChangeConfig,
    compute_correlation_regime_change,
)
from alphamind.distillation.q7.cross_sector_rotation import (
    ROTATION_NARRATIVE_GROWTH_DRIVEN,
    ROTATION_NARRATIVE_RATE_DRIVEN,
    ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN,
    VELOCITY_SHARP,
    VELOCITY_SLOW,
    compute_cross_sector_rotation,
)
from alphamind.distillation.q7.intermarket_regime import (
    GLD_TICKER,
    OIL_SERIES,
    OIL_SOURCE,
    REAL_YIELD_SERIES,
    REAL_YIELD_SOURCE,
    SPY_TICKER,
    TLT_TICKER,
    VIX_SERIES,
    VIX_SOURCE,
    XLE_TICKER,
    compute_intermarket_regime,
)
from alphamind.distillation.q7.intra_sector_correlation import (
    compute_intra_sector_correlation,
)
from alphamind.distillation.q7.lead_lag import (
    LEAD_LAG_LOOKBACK_DEFAULT_DAYS,
    LeadLagPair,
    compute_lead_lag,
)

__all__ = [
    "EMA_WINDOWS_DAYS",
    "GLD_TICKER",
    "LEAD_LAG_LOOKBACK_DEFAULT_DAYS",
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
