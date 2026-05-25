"""Breach-behavior package: zones, forced reduction, halt mode, cascades, emergencies.

Public surface: configuration knobs, canonical typed value objects, and (in
later stories) the primitives that compose them. Re-exports below give every
downstream consumer a single import path.

The historic load-order ``ImportError`` concern formerly documented here is
now mechanically foreclosed by the ``portfolio_state-not-risk_guardrails``
forbidden contract in ``.importlinter`` (rationale: ALP-648). Any future
edge from ``portfolio_state`` back into ``breach_behavior`` breaks the lint
chain before it can reach runtime.
"""

from alphamind.risk_guardrails.breach_behavior.cascade import (
    CascadeContext,
    CascadeStepLimitExceeded,
    FollowUpBreachSelectorProtocol,
    generate_cascade_id,
    orchestrate_breach_cascade,
    orchestrate_margin_call_cascade,
    search_for_alternate_position,
)
from alphamind.risk_guardrails.breach_behavior.config import (
    BreachBehaviorConfig,
    load_breach_behavior_config,
)
from alphamind.risk_guardrails.breach_behavior.drawdown_tiers import (
    apply_progressive_tier_overrides,
    classify_cumulative_drawdown_tier,
)
from alphamind.risk_guardrails.breach_behavior.emergency_triggers import (
    DrawdownSample,
    MarginCallEvent,
    evaluate_daily_drawdown_velocity,
    evaluate_emergency_invocation,
    evaluate_margin_call,
    evaluate_multi_rule_breach,
    evaluate_regime_jump,
)
from alphamind.risk_guardrails.breach_behavior.engine_envelope import (
    command_id_for,
    compose_engine_envelope,
    compose_guardrail_trigger_record,
    envelope_id_for,
)
from alphamind.risk_guardrails.breach_behavior.halt_state import compute_halt_state
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
    BreachDetails,
    BreachResponse,
    CloseRationaleType,
    Direction,
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
    RiskManagementSubtype,
    RiskZone,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
)
from alphamind.risk_guardrails.breach_behavior.zones import classify_zone

__all__ = [
    "BreachBehaviorConfig",
    "BreachDetails",
    "BreachResponse",
    "CascadeContext",
    "CascadeStepLimitExceeded",
    "CloseRationaleType",
    "Direction",
    "DrawdownSample",
    "DrawdownTier",
    "EmergencyContext",
    "EmergencyTrigger",
    "EnforcementTier",
    "EngineCloseCommand",
    "EngineEnvelope",
    "EngineGuardrailTriggerRecord",
    "EscalationZones",
    "EvaluateProposalsCallable",
    "FollowUpBreachSelectorProtocol",
    "HaltState",
    "HardRejectionPayload",
    "InstrumentType",
    "LibraryConfigProtocol",
    "LibraryOutputProtocol",
    "MarginCallEvent",
    "MarketInputsProtocol",
    "PortfolioStateSnapshotProtocol",
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
    "RiskManagementSubtype",
    "RiskZone",
    "RuleProjectionProtocol",
    "SecondaryBreachCheckResult",
    "SecondaryBreachOutcome",
    "apply_progressive_tier_overrides",
    "check_secondary_breach",
    "classify_cumulative_drawdown_tier",
    "classify_zone",
    "command_id_for",
    "compose_engine_envelope",
    "compose_guardrail_trigger_record",
    "compose_hard_rejection_payload",
    "compute_halt_state",
    "envelope_id_for",
    "evaluate_daily_drawdown_velocity",
    "evaluate_emergency_invocation",
    "evaluate_margin_call",
    "evaluate_multi_rule_breach",
    "evaluate_regime_jump",
    "generate_cascade_id",
    "load_breach_behavior_config",
    "orchestrate_breach_cascade",
    "orchestrate_margin_call_cascade",
    "search_for_alternate_position",
    "select_for_drawdown_breach",
    "select_for_margin_call",
    "select_for_position_max_loss",
    "select_for_single_short_max_size_breach",
    "select_for_total_short_exposure_breach",
]
