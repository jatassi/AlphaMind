"""Public surface for the state-delivery rendering layer.

External callers import every name from this module — sub-module imports are
considered private. The state-delivery layer carries the agent-facing guardrail
state header rendering primitives plus the deterministic guardrail validation
tool that the analyst, strategist, and PM compose during reasoning.
"""

from alphamind.risk_guardrails.breach_behavior import (
    EmergencyContext,
    EmergencyTrigger,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    Greeks,
    RuleProjection,
)
from alphamind.risk_guardrails.state_delivery.analyst import render_analyst_header
from alphamind.risk_guardrails.state_delivery.emergency import (
    prepend_emergency_block,
    render_emergency_block,
)
from alphamind.risk_guardrails.state_delivery.strategist import render_strategist_header
from alphamind.risk_guardrails.state_delivery.validation_tool import (
    ProjectedDelta,
    ValidationAction,
    ValidationInstrument,
    ValidationRequest,
    ValidationResult,
    ValidationSize,
    ValidationStrategyLeg,
    ValidationToolState,
    validate_guardrail,
)

__all__ = [
    "EmergencyContext",
    "EmergencyTrigger",
    "FeatureFlagsView",
    "Greeks",
    "ProjectedDelta",
    "RuleProjection",
    "ValidationAction",
    "ValidationInstrument",
    "ValidationRequest",
    "ValidationResult",
    "ValidationSize",
    "ValidationStrategyLeg",
    "ValidationToolState",
    "prepend_emergency_block",
    "render_analyst_header",
    "render_emergency_block",
    "render_strategist_header",
    "validate_guardrail",
]
