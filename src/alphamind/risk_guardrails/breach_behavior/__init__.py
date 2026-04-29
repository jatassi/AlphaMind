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
<<<<<<< HEAD
from alphamind.risk_guardrails.breach_behavior.halt_state import compute_halt_state
=======
from alphamind.risk_guardrails.breach_behavior.emergency_triggers import (
    DrawdownSample,
    MarginCallEvent,
    evaluate_daily_drawdown_velocity,
    evaluate_emergency_invocation,
    evaluate_margin_call,
    evaluate_multi_rule_breach,
    evaluate_regime_jump,
)
>>>>>>> worktree-agent-a2fcf3a1e6a5c40b6
from alphamind.risk_guardrails.breach_behavior.hard_rejection import (
    LibraryOutputProtocol,
    RuleProjectionProtocol,
    compose_hard_rejection_payload,
)
from alphamind.risk_guardrails.breach_behavior.position_selection import (
    PositionLiquidity,
    PositionRiskReward,
    select_for_drawdown_breach,
    select_for_margin_call,
    select_for_position_max_loss,
    select_for_single_short_max_size_breach,
    select_for_total_short_exposure_breach,
)
from alphamind.risk_guardrails.breach_behavior.secondary_breach import (
    EvaluateProposalsCallable,
    LibraryConfigProtocol,
    MarketInputsProtocol,
    PortfolioStateSnapshotProtocol,
    ProposedClose,
    ProposedDeltaProtocol,
    check_secondary_breach,
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
from alphamind.risk_guardrails.breach_behavior.zones import classify_zone

__all__ = [
    "ActiveRiskParameterSet",
    "BreachBehaviorConfig",
    "BreachDetails",
    "BreachResponse",
    "CloseRationaleType",
    "Direction",
    "DrawdownSample",
    "DrawdownState",
    "DrawdownTier",
    "EmergencyContext",
    "EmergencyTrigger",
    "EnforcementTier",
    "EngineCloseCommand",
    "EngineEnvelope",
    "EngineGuardrailTriggerRecord",
    "EscalationZones",
    "EvaluateProposalsCallable",
    "HaltState",
    "HardRejectionPayload",
    "InstrumentType",
    "LibraryConfigProtocol",
    "LibraryOutputProtocol",
<<<<<<< HEAD
    "MarketInputsProtocol",
    "PortfolioStateSnapshotProtocol",
=======
    "MarginCallEvent",
>>>>>>> worktree-agent-a2fcf3a1e6a5c40b6
    "PositionLiquidity",
    "PositionRecord",
    "PositionRiskReward",
    "PositionSelectionAction",
    "PositionSelectionResult",
    "ProgressiveTier",
    "ProposedClose",
    "ProposedDeltaProtocol",
    "RegimeLabel",
    "RegimeTransitionState",
    "RejectionRuleEntry",
    "RiskBudgetConsumption",
    "RiskBudgetEntry",
    "RiskManagementSubtype",
    "RiskZone",
    "RuleProjectionProtocol",
    "SecondaryBreachCheckResult",
    "SecondaryBreachOutcome",
    "apply_progressive_tier_overrides",
    "check_secondary_breach",
    "classify_cumulative_drawdown_tier",
    "classify_zone",
    "compose_hard_rejection_payload",
<<<<<<< HEAD
    "compute_halt_state",
=======
    "evaluate_daily_drawdown_velocity",
    "evaluate_emergency_invocation",
    "evaluate_margin_call",
    "evaluate_multi_rule_breach",
    "evaluate_regime_jump",
>>>>>>> worktree-agent-a2fcf3a1e6a5c40b6
    "load_breach_behavior_config",
    "select_for_drawdown_breach",
    "select_for_margin_call",
    "select_for_position_max_loss",
    "select_for_single_short_max_size_breach",
    "select_for_total_short_exposure_breach",
]
