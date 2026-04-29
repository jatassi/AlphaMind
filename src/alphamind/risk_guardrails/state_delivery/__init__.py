"""Public surface for the state-delivery rendering layer.

External callers import every name from this module — sub-module imports are
considered private. The state-delivery layer carries the agent-facing guardrail
state header rendering primitives plus the deterministic guardrail validation
tool that the analyst, strategist, and PM compose during reasoning.
"""

from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    Greeks,
    RuleProjection,
)
from alphamind.risk_guardrails.state_delivery.analyst import render_analyst_header
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
    "render_analyst_header",
    "validate_guardrail",
]
