"""Pydantic models for regimes/*.yaml.

Each regime declares the multiplier table applied to every guardrail rule for
the operative VIX band, plus the transition mechanics. The distillation layer
classifies the active regime at runtime; story 05's resolver composes the
profile base value with the active regime's multiplier into the resolved limit.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

# Multiplier keys are guardrail rule IDs; the convention matches the
# `_RULE_ID_PATTERN` declared in `models/guardrails.py` (story 03f).
_RULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class Regime(StrEnum):
    low_vol = "low_vol"
    normal = "normal"
    elevated = "elevated"
    crisis = "crisis"


class TightenOnEntry(StrEnum):
    immediate = "immediate"


class LoosenOnExit(StrEnum):
    linear_over_invocations_3 = "linear_over_invocations_3"


class TransitionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    tighten_on_entry: TightenOnEntry
    loosen_on_exit: LoosenOnExit


class RegimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    vix_range: tuple[float, float]
    multipliers: dict[str, float]
    transition: TransitionPolicy

    @model_validator(mode="after")
    def vix_range_is_well_formed(self) -> "RegimeConfig":
        lower, upper = self.vix_range
        if lower < 0 or upper < 0:
            raise ValueError(f"vix_range entries must be ≥ 0, got [{lower}, {upper}]")
        if lower > upper:
            raise ValueError(f"vix_range lower bound must be ≤ upper bound, got [{lower}, {upper}]")
        return self

    @model_validator(mode="after")
    def multipliers_are_well_formed(self) -> "RegimeConfig":
        if not self.multipliers:
            raise ValueError("multipliers must declare at least one entry")
        for rule_id, value in self.multipliers.items():
            if not _RULE_ID_PATTERN.match(rule_id):
                raise ValueError(
                    f"multiplier key {rule_id!r} does not match {_RULE_ID_PATTERN.pattern!r}"
                )
            if value <= 0:
                raise ValueError(
                    f"multiplier {rule_id!r} must be > 0 (zero or negative inverts the rule), "
                    f"got {value}"
                )
        return self
