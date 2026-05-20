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
from alphamind.risk_guardrails.state_delivery.halt_mode import (
    render_analyst_header_halt_mode,
    render_pm_header_halt_mode,
    render_strategist_header_halt_mode,
)
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    CorrelationState,
    CrossConstraintImpact,
    CrossConstraintImpactPerRule,
    DependencyRiskFlag,
    RegimeOverride,
    render_pm_header,
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
    ValidationToolError,
    ValidationToolState,
    ValidationUnavailableReason,
    validate_guardrail,
)
from alphamind.risk_guardrails.state_delivery.validation_tool_mcp import (
    build_initial_validation_state,
    build_validate_guardrail_mcp_server,
)

__all__ = [
    "CorrelationState",
    "CrossConstraintImpact",
    "CrossConstraintImpactPerRule",
    "DependencyRiskFlag",
    "EmergencyContext",
    "EmergencyTrigger",
    "FeatureFlagsView",
    "Greeks",
    "ProjectedDelta",
    "RegimeOverride",
    "RuleProjection",
    "ValidationAction",
    "ValidationInstrument",
    "ValidationRequest",
    "ValidationResult",
    "ValidationSize",
    "ValidationStrategyLeg",
    "ValidationToolError",
    "ValidationToolState",
    "ValidationUnavailableReason",
    "build_initial_validation_state",
    "build_validate_guardrail_mcp_server",
    "prepend_emergency_block",
    "render_analyst_header",
    "render_analyst_header_halt_mode",
    "render_emergency_block",
    "render_pm_header",
    "render_pm_header_halt_mode",
    "render_strategist_header",
    "render_strategist_header_halt_mode",
    "validate_guardrail",
]
