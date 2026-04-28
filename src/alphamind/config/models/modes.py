"""Pydantic models for `config/modes/` bundle (story 04c).

Modes are runtime-resolved by pipeline state — `halt` activates on daily
drawdown halt or cumulative-drawdown tier-3 full halt; `normal` is the default.
Each file declares behavioral transforms applied to decision-layer agents
(analyst, strategist, PM): output-mode flag, action-vocabulary restriction,
and default treatment of pending orders.

`normal.yaml` is a passthrough — every field carries the unrestricted default.
Storing the passthrough explicitly keeps the resolver's mode-application step
uniform across modes (no `if mode == "normal": skip` branches).

The mode contract is decision-layer-only by design (per
`docs/design/06-risk-guardrails/state-delivery.md` § Halt-mode header
modifications). Analysis-layer agents are not mode-affected; the per-agent
block names here are model fields, not validated against `AgentName`.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Mode(StrEnum):
    """Closed set of operational modes; member values match filename stems."""

    normal = "normal"
    halt = "halt"


class AnalystOutputMode(StrEnum):
    proposals = "proposals"
    watchlist = "watchlist"


class StrategistOutputMode(StrEnum):
    normal = "normal"
    defensive_posture = "defensive_posture"


class StrategistAction(StrEnum):
    """Closed strategist action vocabulary.

    `adjust_bracket` carries the hyphenated value `adjust-bracket` to match
    the design doc's worked example (`allowed_actions: [hold, reduce, close,
    adjust-bracket]`). The Python identifier uses underscore; the YAML/string
    value uses hyphen.
    """

    hold = "hold"
    reduce = "reduce"
    close = "close"
    adjust_bracket = "adjust-bracket"
    add = "add"


class PendingOrdersDefault(StrEnum):
    maintain = "maintain"
    cancel = "cancel"


class CommandType(StrEnum):
    """OMS command-type vocabulary; uppercase per the OMS schema convention."""

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    ADJUST = "ADJUST"
    CANCEL = "CANCEL"
    ADD = "ADD"


class PmEmphasis(StrEnum):
    normal = "normal"
    capital_preservation = "capital_preservation"


class AnalystMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    output_mode: AnalystOutputMode


class StrategistMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    output_mode: StrategistOutputMode
    allowed_actions: list[StrategistAction] = Field(min_length=1)
    pending_orders_default: PendingOrdersDefault

    @field_validator("allowed_actions")
    @classmethod
    def allowed_actions_have_no_duplicates(
        cls, v: list[StrategistAction]
    ) -> list[StrategistAction]:
        if len(set(v)) != len(v):
            raise ValueError(f"allowed_actions must not contain duplicates, got {v}")
        return v


class PmMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed_command_types: list[CommandType] = Field(min_length=1)
    emphasis: PmEmphasis

    @field_validator("allowed_command_types")
    @classmethod
    def allowed_command_types_have_no_duplicates(cls, v: list[CommandType]) -> list[CommandType]:
        if len(set(v)) != len(v):
            raise ValueError(f"allowed_command_types must not contain duplicates, got {v}")
        return v


class ModeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    analyst: AnalystMode
    strategist: StrategistMode
    pm: PmMode
