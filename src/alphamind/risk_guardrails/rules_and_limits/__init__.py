"""Public surface for the rules-and-limits subpackage."""

from alphamind.risk_guardrails.rules_and_limits.profile_boundary import (
    ProfileBoundaryEvaluation,
    ProfileBoundaryStatus,
    evaluate_profile_boundary,
)
from alphamind.risk_guardrails.rules_and_limits.registry import (
    ResolvedRule,
    RuleRegistry,
    build_rule_registry,
    iter_active_rules,
)

__all__ = [
    "ProfileBoundaryEvaluation",
    "ProfileBoundaryStatus",
    "ResolvedRule",
    "RuleRegistry",
    "build_rule_registry",
    "evaluate_profile_boundary",
    "iter_active_rules",
]
