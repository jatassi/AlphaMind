"""Public surface for the regime-adaptation feature (story 02 onward).

This story (02) lands the typed value objects every downstream story consumes;
subsequent stories drop function modules into this package and add their own
re-exports below.

The historic load-order ``ImportError`` concern fka documented here is now
mechanically foreclosed by the ``portfolio_state-not-risk_guardrails``
forbidden contract in ``.importlinter`` (rationale: ALP-648). Any future
edge from ``portfolio_state`` back into ``regime_adaptation`` breaks the
lint chain before it can reach runtime.
"""

from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
    build_active_risk_parameters,
    load_prior_active_risk_parameters,
)
from alphamind.risk_guardrails.regime_adaptation.breach_detector import (
    detect_regime_transition_breaches,
)
from alphamind.risk_guardrails.regime_adaptation.event_calendar import (
    EventCalendarParseError,
    load_event_calendar,
    select_events_within_window,
    warn_on_stale_calendar,
)
from alphamind.risk_guardrails.regime_adaptation.inputs_assembly import (
    RegimeAdaptationConfigFan,
    build_inputs_from_distillation_outputs,
    build_inputs_from_persisted_state,
    build_rule_metadata,
    load_config_fan,
)
from alphamind.risk_guardrails.regime_adaptation.interpolation import (
    interpolate_loosening_multipliers,
    resolve_active_multipliers,
)
from alphamind.risk_guardrails.regime_adaptation.orchestrator import resolve_regime_adaptation
from alphamind.risk_guardrails.regime_adaptation.overlay_evaluation import (
    evaluate_pre_event_decision,
    evaluate_stress_decision,
)
from alphamind.risk_guardrails.regime_adaptation.parameter_set import (
    assemble_active_risk_parameter_set,
)
from alphamind.risk_guardrails.regime_adaptation.persistence import (
    insert_state,
    select_most_recent_state,
)
from alphamind.risk_guardrails.regime_adaptation.pre_event_activator import (
    evaluate_pre_event_overlay,
)
from alphamind.risk_guardrails.regime_adaptation.regime_mapping import (
    from_regime_classification,
    map_distillation_to_guardrail_regime,
)
from alphamind.risk_guardrails.regime_adaptation.repository_providers import (
    make_repository_providers,
)
from alphamind.risk_guardrails.regime_adaptation.stress_activator import (
    evaluate_stress_overlay,
    fetch_composite_alert_state,
)
from alphamind.risk_guardrails.regime_adaptation.transition_machine import (
    compute_next_transition,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    LOOSENING_INVOCATIONS,
    CompositeAlertState,
    EventCalendar,
    EventCalendarEntry,
    NextTransitionDecision,
    OverlayActivationDecision,
    RegimeAdaptationAuditEntry,
    RegimeAdaptationAuditEventKind,
    RegimeAdaptationInputs,
    RegimeAdaptationOutput,
    RegimeAdaptationState,
    RegimeTransitionBreach,
    RuleMetadata,
    StaleCalendarReport,
    VixBoundaryThresholds,
    overlays_to_strings,
)

__all__ = [
    "LOOSENING_INVOCATIONS",
    "CompositeAlertState",
    "EventCalendar",
    "EventCalendarEntry",
    "EventCalendarParseError",
    "NextTransitionDecision",
    "OverlayActivationDecision",
    "RegimeAdaptationAuditEntry",
    "RegimeAdaptationAuditEventKind",
    "RegimeAdaptationConfigFan",
    "RegimeAdaptationInputs",
    "RegimeAdaptationOutput",
    "RegimeAdaptationState",
    "RegimeTransitionBreach",
    "RuleMetadata",
    "StaleCalendarReport",
    "VixBoundaryThresholds",
    "assemble_active_risk_parameter_set",
    "build_active_risk_parameters",
    "build_inputs_from_distillation_outputs",
    "build_inputs_from_persisted_state",
    "build_rule_metadata",
    "compute_next_transition",
    "detect_regime_transition_breaches",
    "evaluate_pre_event_decision",
    "evaluate_pre_event_overlay",
    "evaluate_stress_decision",
    "evaluate_stress_overlay",
    "fetch_composite_alert_state",
    "from_regime_classification",
    "insert_state",
    "interpolate_loosening_multipliers",
    "load_config_fan",
    "load_event_calendar",
    "load_prior_active_risk_parameters",
    "make_repository_providers",
    "map_distillation_to_guardrail_regime",
    "overlays_to_strings",
    "resolve_active_multipliers",
    "resolve_regime_adaptation",
    "select_events_within_window",
    "select_most_recent_state",
    "warn_on_stale_calendar",
]
