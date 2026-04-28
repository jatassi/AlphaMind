"""Pydantic models for profiles/<profile>.yaml — operator-pinned identity (story 04a).

A profile names which features are enabled, which sectors are active, which
guardrail rule values apply, and what token-budget ranges decision agents observe
at this capital tier. Per-profile values are authoritative in
`docs/design/06-risk-guardrails/rules-and-limits.md`. Cross-reference checks
(`rule_values` keys exist in guardrails.yaml; `active_sectors` exist in
assets.yaml; `agent_token_budgets` keys are members of `AgentName`) live in
story 06a; feature-flag closure (no options rules in a profile with
`options_enabled: false`) and capital-range non-overlap live in story 06b.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Sector and rule keys are lowercase identifiers using underscores; the
# convention is documented in `docs/design/configuration-management.md` and
# `docs/design/06-risk-guardrails/rules-and-limits.md`.
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class RiskPriority(StrEnum):
    """Operator-facing risk-management priority per profile.

    Values are normalized snake_case from the prose phrases in
    `docs/design/06-risk-guardrails/rules-and-limits.md` (signal quality,
    concentration management, exposure management, exposure plus execution).
    """

    signal_quality = "signal_quality"
    concentration_management = "concentration_management"
    exposure_management = "exposure_management"
    exposure_and_execution_management = "exposure_and_execution_management"


class FeatureFlags(BaseModel):
    model_config = ConfigDict(frozen=True)

    options_enabled: bool
    short_selling_enabled: bool
    fractional_shares_required: bool


class TokenBudgetRange(BaseModel):
    """`(lower, upper)` ranges for context and output token budgets.

    The runner consumes the upper bound when sizing the SDK call; the lower
    bound is review-surface metadata.
    """

    model_config = ConfigDict(frozen=True)

    context: tuple[int, int]
    output: tuple[int, int]

    @field_validator("context", "output")
    @classmethod
    def lower_le_upper_and_positive(cls, v: tuple[int, int]) -> tuple[int, int]:
        lower, upper = v
        if lower < 1 or upper < 1:
            raise ValueError(f"Token budget bounds must be >= 1, got [{lower}, {upper}]")
        if lower > upper:
            raise ValueError(f"Token budget lower must be <= upper, got [{lower}, {upper}]")
        return v


class ProfileConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    capital_range_usd: tuple[int, int]
    risk_priority: RiskPriority
    feature_flags: FeatureFlags
    active_sectors: list[str]
    min_position_size_usd: int = Field(ge=1)
    rule_values: dict[str, float]
    agent_token_budgets: dict[str, TokenBudgetRange]

    @field_validator("capital_range_usd")
    @classmethod
    def capital_range_is_strict_non_negative_interval(cls, v: tuple[int, int]) -> tuple[int, int]:
        lower, upper = v
        if lower < 0 or lower >= upper:
            raise ValueError(
                f"capital_range_usd must satisfy 0 <= lower < upper, got [{lower}, {upper}]"
            )
        return v

    @field_validator("active_sectors")
    @classmethod
    def active_sectors_non_empty_unique_well_formed(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("active_sectors must not be empty")
        seen: set[str] = set()
        for sector in v:
            if not _KEY_PATTERN.match(sector):
                raise ValueError(
                    f"active_sectors entry {sector!r} does not match {_KEY_PATTERN.pattern!r}"
                )
            if sector in seen:
                raise ValueError(f"active_sectors contains duplicate {sector!r}")
            seen.add(sector)
        return v

    @field_validator("rule_values")
    @classmethod
    def rule_values_non_empty_keys_well_formed(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("rule_values must not be empty")
        for key in v:
            if not _KEY_PATTERN.match(key):
                raise ValueError(f"rule_values key {key!r} does not match {_KEY_PATTERN.pattern!r}")
        return v
