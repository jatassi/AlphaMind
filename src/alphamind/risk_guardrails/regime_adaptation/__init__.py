"""Public surface for the regime-adaptation feature (story 02 onward).

This story (02) lands the typed value objects every downstream story consumes;
subsequent stories drop function modules into this package and add their own
re-exports below.
"""

from alphamind.risk_guardrails.regime_adaptation.interpolation import (
    interpolate_loosening_multipliers,
    resolve_active_multipliers,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    LOOSENING_INVOCATIONS,
    CompositeAlertState,
    EventCalendar,
    EventCalendarEntry,
    NextTransitionDecision,
    OverlayActivationDecision,
    RegimeAdaptationAuditEntry,
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
    "NextTransitionDecision",
    "OverlayActivationDecision",
    "RegimeAdaptationAuditEntry",
    "RegimeAdaptationOutput",
    "RegimeAdaptationState",
    "RegimeTransitionBreach",
    "RuleMetadata",
    "StaleCalendarReport",
    "VixBoundaryThresholds",
    "interpolate_loosening_multipliers",
    "overlays_to_strings",
    "resolve_active_multipliers",
]
