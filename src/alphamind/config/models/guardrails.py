"""Pydantic models for guardrails.yaml.

The registry holds metadata only — enforcement tiers, escalation zones,
breach-response classification, monitor-between-invocations flag, optional
progressive tiers — plus the emergency-invocation trigger registry. Rule
*values* live in profile files; rule *multipliers* live in regime files. Both
reference rule IDs from this file.
"""

import re
from enum import StrEnum
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Rule whose breach-behavior spec mandates an ordered list of progressive
# response tiers. Every other rule must omit `progressive_tiers`.
_PROGRESSIVE_TIER_RULE_ID = "cumulative_drawdown_pct"

# Rule IDs are lowercase identifiers using underscores; the convention is
# documented in `docs/design/configuration-management.md`'s per-profile
# `rule_values` example and locked by story 03f.
_RULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class EnforcementTier(StrEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class BreachResponse(StrEnum):
    immediate_engine = "immediate_engine"
    deferred_to_pm = "deferred_to_pm"


class EscalationZones(BaseModel):
    model_config = ConfigDict(frozen=True)

    warning: int = Field(ge=0, le=100)
    critical: int = Field(ge=0, le=100)
    hard_block: int = Field(ge=0, le=100)


class ProgressiveTier(BaseModel):
    model_config = ConfigDict(frozen=True)

    trigger_pct: float = Field(gt=0)
    max_position_size_pct: float | None = None
    max_gross_pct: float | None = None
    full_halt: bool = False


class RuleEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    enforcement_tiers: list[EnforcementTier] = Field(min_length=1)
    escalation_zones: EscalationZones
    breach_response: BreachResponse
    monitor_between_invocations: bool
    progressive_tiers: list[ProgressiveTier] | None = None


class EmergencyInvocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    cooldown_minutes: int = Field(ge=0)
    # Heterogeneous list mirrors the design doc: bare strings for parameterless
    # triggers and single-key maps for parameterized ones.
    triggers: list[str | dict[str, Any]] = Field(min_length=1)


class GuardrailsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    rules: list[RuleEntry] = Field(min_length=1)
    emergency_invocation: EmergencyInvocation

    @model_validator(mode="after")
    def rule_ids_unique(self) -> "GuardrailsConfig":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"Duplicate rule id {rule.id!r} in guardrails registry")
            seen.add(rule.id)
        return self

    @model_validator(mode="after")
    def rule_ids_match_pattern(self) -> "GuardrailsConfig":
        for rule in self.rules:
            if not _RULE_ID_PATTERN.match(rule.id):
                raise ValueError(f"Rule id {rule.id!r} does not match {_RULE_ID_PATTERN.pattern!r}")
        return self

    @model_validator(mode="after")
    def escalation_zones_strictly_ordered(self) -> "GuardrailsConfig":
        for rule in self.rules:
            zones = rule.escalation_zones
            if not (zones.warning < zones.critical < zones.hard_block):
                raise ValueError(
                    f"Rule {rule.id!r} escalation_zones must satisfy "
                    f"warning < critical < hard_block, got "
                    f"{zones.warning}/{zones.critical}/{zones.hard_block}"
                )
        return self

    @model_validator(mode="after")
    def progressive_tiers_only_on_cumulative_drawdown(self) -> "GuardrailsConfig":
        for rule in self.rules:
            if rule.id == _PROGRESSIVE_TIER_RULE_ID:
                if not rule.progressive_tiers:
                    raise ValueError(
                        f"Rule {rule.id!r} requires progressive_tiers with at least one entry"
                    )
                triggers = [tier.trigger_pct for tier in rule.progressive_tiers]
                if any(b <= a for a, b in pairwise(triggers)):
                    raise ValueError(
                        f"Rule {rule.id!r} progressive_tiers must be monotonically "
                        f"increasing in trigger_pct, got {triggers}"
                    )
            elif rule.progressive_tiers is not None:
                raise ValueError(
                    f"Rule {rule.id!r} must not declare progressive_tiers; "
                    f"only {_PROGRESSIVE_TIER_RULE_ID!r} carries that field"
                )
        return self
