"""Q6 macro indicators and funding-stress composite (story 02-distillation-layer/08c).

Implements the deterministic macro computations from
``docs/design/02-distillation-layer/external.md`` § 2 From macro and rates
(quant 6):

- :func:`classify_yield_curve_regime` — quant 6a yield-curve regime
  classifier (five labels plus transition flag).
- :func:`classify_inflation_regime` — quant 6c inflation regime classifier
  (four labels plus transition flag).
- :func:`classify_dollar_attribution` — quant 6f dollar-move attribution
  (three labels by largest absolute correlation over a trailing window).
- :func:`refresh_funding_stress_composite` — quant 6e four-component
  funding-stress composite, persisted via story 07's
  ``refresh_composite_state``.
- :func:`refresh_market_liquidity_composite` — market-wide liquidity
  composite (the partner of funding stress), persisted via story 07.
- :func:`detect_macro_surprise_anomaly` — macro release surprise anomaly
  per ``external.md`` § 3 Anomaly detection.
- :func:`assemble_q6_blocks` — pack the above into ``OutputBlock`` instances
  with ``audience = UNIVERSAL_BROADCAST``.

ALP-485 split the implementation along the compute/load boundary:

- :mod:`alphamind.distillation.q6._loaders` — IO shell with all session-bound
  FRED reads, the composite-refresh writes, and the macro-release surprise
  scanner. Returns a frozen :class:`Q6Inputs`.
- :mod:`alphamind.distillation.q6.{yield_curve,inflation,dollar_attribution,
  funding_stress,market_liquidity,macro_surprise}_compute` — pure compute
  cores, pinned ORM-free by the import-linter contract.
- :mod:`alphamind.distillation.q6.assemble` — pure
  :func:`assemble_q6_blocks_from_inputs` (the function Phase 2 parallelism
  calls) plus :func:`compute_q6_blocks` (session-accepting shim).
"""

from __future__ import annotations

from alphamind.distillation.q6._loaders import (
    Q6Inputs,
    load_q6_inputs,
    refresh_funding_stress_composite,
    refresh_market_liquidity_composite,
)
from alphamind.distillation.q6.assemble import (
    assemble_q6_blocks,
    assemble_q6_blocks_from_inputs,
    compute_q6_blocks,
)
from alphamind.distillation.q6.dollar_attribution_compute import (
    DollarAttributionLabel,
    DollarAttributionResult,
    classify_dollar_attribution,
)
from alphamind.distillation.q6.funding_stress_compute import (
    FUNDING_STRESS_COMPONENT_NAMES,
    FUNDING_STRESS_COMPOSITE_KIND,
    FundingStressResult,
)
from alphamind.distillation.q6.inflation_compute import (
    InflationRegimeLabel,
    InflationRegimeResult,
    classify_inflation_regime,
)
from alphamind.distillation.q6.macro_surprise_compute import detect_macro_surprise_anomaly
from alphamind.distillation.q6.market_liquidity_compute import (
    MARKET_LIQUIDITY_COMPOSITE_KIND,
    CompositeMethod,
    MarketLiquidityResult,
)
from alphamind.distillation.q6.yield_curve_compute import (
    YieldCurveRegimeLabel,
    YieldCurveRegimeResult,
    classify_yield_curve_regime,
)

__all__ = [
    "FUNDING_STRESS_COMPONENT_NAMES",
    "FUNDING_STRESS_COMPOSITE_KIND",
    "MARKET_LIQUIDITY_COMPOSITE_KIND",
    "CompositeMethod",
    "DollarAttributionLabel",
    "DollarAttributionResult",
    "FundingStressResult",
    "InflationRegimeLabel",
    "InflationRegimeResult",
    "MarketLiquidityResult",
    "Q6Inputs",
    "YieldCurveRegimeLabel",
    "YieldCurveRegimeResult",
    "assemble_q6_blocks",
    "assemble_q6_blocks_from_inputs",
    "classify_dollar_attribution",
    "classify_inflation_regime",
    "classify_yield_curve_regime",
    "compute_q6_blocks",
    "detect_macro_surprise_anomaly",
    "load_q6_inputs",
    "refresh_funding_stress_composite",
    "refresh_market_liquidity_composite",
]
