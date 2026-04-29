"""Rules-and-limits feature surface.

Public re-exports for the rule-registry runtime accessor (story 01a). The
registry is a thin, immutable lookup over ``GuardrailsConfig.rules`` shared
across guardrail consumers.
"""

from alphamind.risk_guardrails.rules_and_limits.registry import (
    ResolvedRule,
    RuleRegistry,
    build_rule_registry,
    iter_active_rules,
)

__all__ = [
    "ResolvedRule",
    "RuleRegistry",
    "build_rule_registry",
    "iter_active_rules",
]
