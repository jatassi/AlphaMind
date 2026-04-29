"""Public surface for the rules-and-limits subpackage."""

from alphamind.risk_guardrails.rules_and_limits.min_position_size import (
    MinPositionSizeResult,
    MinPositionSizeStatus,
    check_min_position_size,
)
from alphamind.risk_guardrails.rules_and_limits.profile_boundary import (
    ProfileBoundaryEvaluation,
    ProfileBoundaryStatus,
    evaluate_profile_boundary,
)
from alphamind.risk_guardrails.rules_and_limits.profile_switch import (
    ProfileNotFoundError,
    ProfileSwitchOutcome,
    build_profile_switch_activity_log_entry,
    switch_active_profile,
)
from alphamind.risk_guardrails.rules_and_limits.registry import (
    ResolvedRule,
    RuleRegistry,
    build_rule_registry,
    iter_active_rules,
)

__all__ = [
    "MinPositionSizeResult",
    "MinPositionSizeStatus",
    "ProfileBoundaryEvaluation",
    "ProfileBoundaryStatus",
    "ProfileNotFoundError",
    "ProfileSwitchOutcome",
    "ResolvedRule",
    "RuleRegistry",
    "build_profile_switch_activity_log_entry",
    "build_rule_registry",
    "check_min_position_size",
    "evaluate_profile_boundary",
    "iter_active_rules",
    "switch_active_profile",
]
