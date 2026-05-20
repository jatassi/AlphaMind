"""Q6 macro indicators and funding-stress composite — re-export shim (ALP-485).

ALP-485 split the implementation into the ``alphamind.distillation.q6``
sub-package along the compute/load boundary. This module survives as a
thin re-export shim so existing call sites — the orchestrator's
``compute_q6_blocks`` import and the external test suite at
``tests/distillation/external/test_q6_*`` — keep working without touching
the package path. New code should import from
:mod:`alphamind.distillation.q6` directly.
"""

from __future__ import annotations

from alphamind.distillation.q6 import (
    FUNDING_STRESS_COMPONENT_NAMES,
    FUNDING_STRESS_COMPOSITE_KIND,
    MARKET_LIQUIDITY_COMPOSITE_KIND,
    CompositeMethod,
    DollarAttributionLabel,
    DollarAttributionResult,
    FundingStressResult,
    InflationRegimeLabel,
    InflationRegimeResult,
    MarketLiquidityResult,
    Q6Inputs,
    YieldCurveRegimeLabel,
    YieldCurveRegimeResult,
    assemble_q6_blocks,
    assemble_q6_blocks_from_inputs,
    classify_dollar_attribution,
    classify_inflation_regime,
    classify_yield_curve_regime,
    compute_q6_blocks,
    detect_macro_surprise_anomaly,
    load_q6_inputs,
    refresh_funding_stress_composite,
    refresh_market_liquidity_composite,
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
