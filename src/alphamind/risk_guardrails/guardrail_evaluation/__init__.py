"""Public surface for the guardrail-evaluation library (story 01).

External callers import every name from this module — sub-module imports are
considered private. The library itself is a pure-function math layer with
three callers (the agent-side validation tool, the proposal pre-processor's
combined-set check, and the engine T3 enforcement check); story 01 lays the
boundary contract every later story builds on.
"""

from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_greeks
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    ContractType,
    DeltaAdjustedExposure,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureDisabledRejection,
    FeatureFlagsView,
    Greeks,
    IvProvider,
    IvSource,
    LibraryConfig,
    LibraryOutput,
    MarketInputs,
    OptionLeg,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleProjection,
    Status,
)

# Drop the implicit submodule attributes the import system populates, so
# ``dir(...)`` reflects the documented re-export list verbatim — the
# acceptance test asserts the surface is exactly the names below and nothing
# more.
globals().pop("black_scholes", None)
globals().pop("types", None)

__all__ = [
    "Action",
    "AssetType",
    "ContractType",
    "DeltaAdjustedExposure",
    "Direction",
    "EscalationZones",
    "ExistingPosition",
    "FeatureDisabledRejection",
    "FeatureFlagsView",
    "Greeks",
    "IvProvider",
    "IvSource",
    "LibraryConfig",
    "LibraryOutput",
    "MarketInputs",
    "OptionLeg",
    "PortfolioStateSnapshot",
    "ProposedDelta",
    "RuleProjection",
    "Status",
    "bs_greeks",
]
