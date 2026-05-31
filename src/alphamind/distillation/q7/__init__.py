"""Q7 cross-asset and correlation computations.

ALP-486 split this sub-package along the compute/load boundary so the
orchestrator's Phase 2 can run q7 in parallel with q1 / q3 / q6 /
qualitative. The implementation is split across:

- :mod:`alphamind.distillation.q7._helpers` — shared pure math primitives
  (correlation, log-return, z-score, lagged-correlation helpers).
- :mod:`alphamind.distillation.q7._loaders` — IO shell: ``Q7Inputs`` +
  ``load_q7_inputs``; owns every session-bound read plus the intra-sector
  ``correlation_divergence`` event writes.
- :mod:`alphamind.distillation.q7.intra_sector_correlation` and the
  five other detection sub-modules — thin orchestration that preserves
  the legacy session-accepting public API by delegating to the per-sub
  ``*_compute.py`` pure cores.
- :mod:`alphamind.distillation.q7.assemble` — pure
  :func:`assemble_q7_blocks_from_inputs` plus the
  ``Session``-accepting shim :func:`assemble_q7_blocks`.
"""

from __future__ import annotations

from alphamind.distillation.q7._loaders import (
    Q7Inputs,
    compute_pair_correlations,
    load_q7_inputs,
)
from alphamind.distillation.q7.assemble import (
    assemble_q7_blocks,
    assemble_q7_blocks_from_inputs,
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
    "Q7Inputs",
    "assemble_q7_blocks",
    "assemble_q7_blocks_from_inputs",
    "compute_breadth_internals",
    "compute_correlation_regime_change",
    "compute_cross_sector_rotation",
    "compute_intermarket_regime",
    "compute_intra_sector_correlation",
    "compute_lead_lag",
    "compute_pair_correlations",
    "load_q7_inputs",
]
