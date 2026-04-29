"""Public surface for the guardrail-evaluation library (story 01).

External callers import every name from this module — sub-module imports are
considered private. The library itself is a pure-function math layer with
three callers (the agent-side validation tool, the proposal pre-processor's
combined-set check, and the engine T3 enforcement check); story 01 lays the
boundary contract every later story builds on.
"""

from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_greeks
from alphamind.risk_guardrails.guardrail_evaluation.delta_adjusted import (
    compute_delta_adjusted_exposure,
)
from alphamind.risk_guardrails.guardrail_evaluation.effective_limits import (
    EffectiveLimitAdapterError,
    from_resolved_config,
)
from alphamind.risk_guardrails.guardrail_evaluation.feature_gate import (
    classify_feature_gate,
)
from alphamind.risk_guardrails.guardrail_evaluation.iv_sourcing import (
    FixtureIvProvider,
    IvLookupError,
    IvLookupResult,
    IvProvider,
    IvQuote,
    IvSurfaceEntry,
    RealizedVolEntry,
)
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
for _submodule in (
    "black_scholes",
    "delta_adjusted",
    "effective_limits",
    "feature_gate",
    "iv_sourcing",
    "types",
):
    globals().pop(_submodule, None)
del _submodule

__all__ = [
    "Action",
    "AssetType",
    "ContractType",
    "DeltaAdjustedExposure",
    "Direction",
    "EffectiveLimitAdapterError",
    "EscalationZones",
    "ExistingPosition",
    "FeatureDisabledRejection",
    "FeatureFlagsView",
    "FixtureIvProvider",
    "Greeks",
    "IvLookupError",
    "IvLookupResult",
    "IvProvider",
    "IvQuote",
    "IvSource",
    "IvSurfaceEntry",
    "LibraryConfig",
    "LibraryOutput",
    "MarketInputs",
    "OptionLeg",
    "PortfolioStateSnapshot",
    "ProposedDelta",
    "RealizedVolEntry",
    "RuleProjection",
    "Status",
    "bs_greeks",
    "classify_feature_gate",
    "compute_delta_adjusted_exposure",
    "from_resolved_config",
]
