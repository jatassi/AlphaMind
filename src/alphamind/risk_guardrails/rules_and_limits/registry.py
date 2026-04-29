"""Runtime accessor over ``GuardrailsConfig.rules`` (story 01a).

``GuardrailsConfig`` carries the rule registry as a parsed list. Every
guardrail consumer otherwise rebuilds the same dict to look rules up by ID,
filter by enforcement tier, or pick out the rules the continuous monitor must
re-evaluate. ``RuleRegistry`` centralises that lookup surface so downstream
features (state delivery, breach behavior, guardrail evaluation) share one
stable accessor.

The registry is built once per ``GuardrailsConfig`` and is fully immutable —
the inner mapping is wrapped in ``MappingProxyType`` so the dataclass remains
hashable without converting the dict into a tuple-of-items.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from alphamind.config.models import (
    EnforcementTier,
    GuardrailsConfig,
    RuleEntry,
)
from alphamind.config.resolver import ResolvedConfig


@dataclass(frozen=True, slots=True)
class RuleRegistry:
    """Immutable lookup over the ``GuardrailsConfig.rules`` list.

    Iteration, ``at_tier``, ``requiring_monitor``, and
    ``with_progressive_tiers`` all preserve the ``guardrails.rules``
    declaration order so consumers' rendered output stays legible (the shipped
    YAML groups rules by category for human review).
    """

    _by_id: Mapping[str, RuleEntry]

    def get(self, rule_id: str) -> RuleEntry:
        try:
            return self._by_id[rule_id]
        except KeyError as exc:
            raise KeyError(f"Rule {rule_id!r} is not present in the registry") from exc

    def try_get(self, rule_id: str) -> RuleEntry | None:
        return self._by_id.get(rule_id)

    def at_tier(self, tier: EnforcementTier) -> tuple[RuleEntry, ...]:
        return tuple(rule for rule in self._by_id.values() if tier in rule.enforcement_tiers)

    def requiring_monitor(self) -> tuple[RuleEntry, ...]:
        return tuple(rule for rule in self._by_id.values() if rule.monitor_between_invocations)

    def with_progressive_tiers(self) -> tuple[RuleEntry, ...]:
        return tuple(rule for rule in self._by_id.values() if rule.progressive_tiers is not None)

    def __iter__(self) -> Iterator[RuleEntry]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, rule_id: object) -> bool:
        return rule_id in self._by_id


def build_rule_registry(guardrails: GuardrailsConfig) -> RuleRegistry:
    """Build a ``RuleRegistry`` from a parsed ``GuardrailsConfig``.

    The Pydantic validator on ``GuardrailsConfig`` already enforces ``id``
    uniqueness; the duplicate-ID guard here is defensive insurance against
    callers that bypass parse-time validation (``model_construct`` in tests,
    direct dataclass instantiation by future code).
    """
    by_id: dict[str, RuleEntry] = {}
    for rule in guardrails.rules:
        if rule.id in by_id:
            raise ValueError(
                f"Duplicate rule id {rule.id!r} in GuardrailsConfig.rules; "
                f"this is a structural error normally caught by the parse-time validator"
            )
        by_id[rule.id] = rule
    return RuleRegistry(_by_id=MappingProxyType(by_id))


@dataclass(frozen=True, slots=True)
class ResolvedRule:
    """A rule's metadata paired with its currently-active limit.

    Intentionally minimal — current portfolio state, headroom, and per-invocation
    signals join in at the consumer (state delivery joins them with portfolio
    state §4c; breach behavior joins them with the breach detector). Keeping
    this struct as just ``(metadata, limit)`` makes it reusable across
    consumers without becoming a god object.
    """

    metadata: RuleEntry
    limit: float


def iter_active_rules(resolved: ResolvedConfig, registry: RuleRegistry) -> tuple[ResolvedRule, ...]:
    """Yield one ``ResolvedRule`` per entry in ``resolved.rule_values``.

    Iteration order matches ``resolved.rule_values``. A rule present in
    ``rule_values`` but absent from ``registry`` is a structural error
    post-validation — raise ``KeyError`` naming the offending rule.
    """
    resolved_rules: list[ResolvedRule] = []
    for rule_id, limit in resolved.rule_values.items():
        metadata = registry.try_get(rule_id)
        if metadata is None:
            raise KeyError(
                f"Rule {rule_id!r} present in ResolvedConfig.rule_values but absent "
                f"from the rule registry; this is a structural error post-validation"
            )
        resolved_rules.append(ResolvedRule(metadata=metadata, limit=limit))
    return tuple(resolved_rules)
