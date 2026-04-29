"""Breach-behavior package: zones, forced reduction, halt mode, cascades, emergencies.

Public surface: configuration knobs, canonical typed value objects, and (in
later stories) the primitives that compose them. Re-exports below give every
downstream consumer a single import path.
"""

from alphamind.risk_guardrails.breach_behavior.config import (
    BreachBehaviorConfig,
    load_breach_behavior_config,
)
from alphamind.risk_guardrails.breach_behavior.drawdown_tiers import (
    apply_progressive_tier_overrides,
    classify_cumulative_drawdown_tier,
)
from alphamind.risk_guardrails.breach_behavior.types import (
    ActiveRiskParameterSet,
    BreachDetails,
    BreachResponse,
    CloseRationaleType,
    Direction,
    DrawdownState,
    DrawdownTier,
    EmergencyContext,
    EmergencyTrigger,
    EnforcementTier,
    EngineCloseCommand,
    EngineEnvelope,
    EngineGuardrailTriggerRecord,
    EscalationZones,
    HaltState,
    HardRejectionPayload,
    InstrumentType,
    PositionRecord,
    PositionSelectionAction,
    PositionSelectionResult,
    ProgressiveTier,
    RegimeLabel,
    RegimeTransitionState,
    RejectionRuleEntry,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskManagementSubtype,
    RiskZone,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
)

__all__ = [
    "ActiveRiskParameterSet",
    "BreachBehaviorConfig",
    "BreachDetails",
    "BreachResponse",
    "CloseRationaleType",
    "Direction",
    "DrawdownState",
    "DrawdownTier",
    "EmergencyContext",
    "EmergencyTrigger",
    "EnforcementTier",
    "EngineCloseCommand",
    "EngineEnvelope",
    "EngineGuardrailTriggerRecord",
    "EscalationZones",
    "HaltState",
    "HardRejectionPayload",
    "InstrumentType",
    "PositionRecord",
    "PositionSelectionAction",
    "PositionSelectionResult",
    "ProgressiveTier",
    "RegimeLabel",
    "RegimeTransitionState",
    "RejectionRuleEntry",
    "RiskBudgetConsumption",
    "RiskBudgetEntry",
    "RiskManagementSubtype",
    "RiskZone",
    "SecondaryBreachCheckResult",
    "SecondaryBreachOutcome",
    "apply_progressive_tier_overrides",
    "classify_cumulative_drawdown_tier",
    "load_breach_behavior_config",
]
