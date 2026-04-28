"""Pydantic models for overlays/{pre-event,stress}.yaml (story 04d).

Overlays apply additive multiplicative tightening on top of the active regime:
``pre_event`` tightens before scheduled catalysts (FOMC, CPI, earnings); ``stress``
tightens on funding-stress / liquidity-distress signals from the distillation
layer. Each overlay is runtime-resolved (zero or more active per invocation).

Overlay multipliers are *partial* maps — only the rules they tighten — in
contrast to regime multipliers which cover every rule. The model accepts any
non-empty multipliers map without insisting on full coverage.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Multiplier keys are guardrail rule IDs; the convention is documented in
# `docs/design/configuration-management.md` and locked by story 03f.
_MULTIPLIER_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class Overlay(StrEnum):
    """Closed set of overlay names. Member values match filename stems with
    ``_`` replacing ``-`` (e.g. ``pre_event`` ↔ ``pre-event.yaml``).
    """

    pre_event = "pre_event"
    stress = "stress"


class EventType(StrEnum):
    """Closed set of scheduled-catalyst event types that activate ``pre_event``."""

    fomc = "fomc"
    cpi = "cpi"
    ppi = "ppi"
    pce = "pce"
    nfp = "nfp"
    earnings = "earnings"


class StressTrigger(StrEnum):
    """Closed set of distillation-layer alerts that activate ``stress``."""

    funding_stress_composite = "funding_stress_composite"
    market_liquidity_score = "market_liquidity_score"


class PreEventActivation(BaseModel):
    model_config = ConfigDict(frozen=True)

    windows_before_event: int = Field(ge=1)
    events: list[EventType] = Field(min_length=1)


class StressActivation(BaseModel):
    model_config = ConfigDict(frozen=True)

    triggers: list[StressTrigger] = Field(min_length=1)


class FinalInvocationBeforeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    block_new_positions: bool


def _validate_multipliers(value: dict[str, float]) -> dict[str, float]:
    if not value:
        raise ValueError("multipliers must contain at least one entry")
    for key, multiplier in value.items():
        if not _MULTIPLIER_KEY_PATTERN.match(key):
            raise ValueError(
                f"multiplier key {key!r} does not match {_MULTIPLIER_KEY_PATTERN.pattern!r}"
            )
        if multiplier <= 0:
            raise ValueError(
                f"multiplier {key!r} must be > 0 (zero or negative would invert the rule), "
                f"got {multiplier}"
            )
    return value


class PreEventOverlay(BaseModel):
    model_config = ConfigDict(frozen=True)

    activation: PreEventActivation
    multipliers: dict[str, float]
    final_invocation_before_event: FinalInvocationBeforeEvent

    @field_validator("multipliers")
    @classmethod
    def _check_multipliers(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_multipliers(value)


class StressOverlay(BaseModel):
    model_config = ConfigDict(frozen=True)

    activation: StressActivation
    multipliers: dict[str, float]

    @field_validator("multipliers")
    @classmethod
    def _check_multipliers(cls, value: dict[str, float]) -> dict[str, float]:
        return _validate_multipliers(value)
