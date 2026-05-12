"""Canonical broker-grade Pydantic OMS command models — ALP-370 / story 01a.

Translates ``docs/design/05-execution-layer/oms-command-schema.md`` (JSON Schema
Draft 2020-12) into a discriminated-union Pydantic shape with all required and
optional fields, full sub-records, and per-design ``model_validator``s for the
cross-field invariants the JSON Schema expresses via ``allOf`` / ``anyOf``.

The schema is the contract; this module is its Python counterpart. Every
``$defs`` entry in the schema has a class here, and every ``allOf``/``anyOf``
gate becomes a ``model_validator`` on the relevant variant.

Three structural decisions trace back to the parent issue ALP-120:

* **(A)** ``alphamind.commands`` is the canonical home (formerly
  ``execution.oms.command_models``; relocated by ALP-458 to break the
  decision↔execution import cycle by hoisting the LLM↔engine wire-format
  types into a top-level kernel package).
* **(B)** :class:`PositionSize` carries no ``sector`` field — sector is a
  risk-side concept derived by the guardrail layer.
* **(E)** :class:`CloseCommand` ``close_rationale_type`` matches the design
  enum ``thesis_invalidated | target_reached | conviction_reduced |
  risk_management`` (no transitional ``tactical_exit``).

``command_id`` is optional on every variant: PM-originated envelopes arrive
with ``command_id=None`` and the OMS command intake layer assigns the
structural ID per :doc:`/docs/design/oms-command-ids`. Field-level pattern
enforcement is deferred to the intake (story 03); this module documents the
expected formats in the field docstring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    TypeAdapter,
    model_validator,
)

__all__ = [
    "AddCommand",
    "AdjustCommand",
    "AssetType",
    "BracketAdjustment",
    "BracketOrderParameters",
    "BracketOrderType",
    "CancelCommand",
    "CloseCommand",
    "CloseRationaleType",
    "CommandType",
    "Comparator",
    "ComponentType",
    "ContractType",
    "Direction",
    "EntryOrder",
    "EntryOrderType",
    "EquityInstrument",
    "EventCondition",
    "EventLeg",
    "Instrument",
    "InvalidationLeg",
    "LegType",
    "NewEventInvalidation",
    "NewStopLevel",
    "NewTargetLevel",
    "OMSCommand",
    "OpenCommand",
    "OptionInstrument",
    "PositionSize",
    "PriceCondition",
    "PriceLeg",
    "RiskManagementSubtype",
    "StrategyInstrument",
    "StrategyLeg",
    "StrategyType",
    "Target",
    "TargetType",
    "Thesis",
    "ThesisComponent",
    "TimeCondition",
    "TimeLeg",
    "oms_command_schema",
]


# ---------------------------------------------------------------------------
# Vocabulary literal aliases — wire-format Literal types per the JSON Schema.
# ---------------------------------------------------------------------------


CommandType = Literal["open", "close", "adjust", "cancel", "add"]
AssetType = Literal["equity", "option", "strategy"]
Direction = Literal["long", "short"]
ContractType = Literal["call", "put"]
EntryOrderType = Literal["market", "limit", "stop_limit"]
TargetType = Literal["absolute_price", "pl_percentage", "pl_dollar"]
BracketOrderType = Literal["market", "limit", "stop", "stop_limit"]
LegType = Literal["price", "time", "event"]
Comparator = Literal["<=", ">=", "<", ">"]
CloseRationaleType = Literal[
    "thesis_invalidated",
    "target_reached",
    "conviction_reduced",
    "risk_management",
]
RiskManagementSubtype = Literal["pm_directed", "engine_guardrail"]
ComponentType = Literal["entry_rationale", "target_rationale", "invalidation_rationale"]
StrategyType = Literal[
    "vertical_spread",
    "calendar_spread",
    "straddle",
    "strangle",
    "iron_condor",
    "custom",
]


# ---------------------------------------------------------------------------
# Instrument variants
# ---------------------------------------------------------------------------


class EquityInstrument(BaseModel):
    """Equity instrument — ticker + direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_type: Literal["equity"]
    ticker: str = Field(min_length=1)
    direction: Direction


class OptionInstrument(BaseModel):
    """Single-leg option contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_type: Literal["option"]
    underlying: str = Field(min_length=1)
    strike: float = Field(gt=0)
    expiration: str = Field(min_length=1)
    contract_type: ContractType
    direction: Direction


class StrategyLeg(BaseModel):
    """One leg of a multi-leg options strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strike: float = Field(gt=0)
    expiration: str = Field(min_length=1)
    contract_type: ContractType
    direction: Direction
    quantity_ratio: int = Field(ge=1)


class StrategyInstrument(BaseModel):
    """Multi-leg options strategy with a named ``strategy_type``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_type: Literal["strategy"]
    strategy_type: StrategyType
    underlying: str = Field(min_length=1)
    legs: tuple[StrategyLeg, ...] = Field(min_length=2)


Instrument = Annotated[
    EquityInstrument | OptionInstrument | StrategyInstrument,
    Discriminator("asset_type"),
]


# ---------------------------------------------------------------------------
# Order / sizing / target sub-records
# ---------------------------------------------------------------------------


class EntryOrder(BaseModel):
    """Entry order parameters with conditional price requirements per ``type``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EntryOrderType
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_invariants(self) -> EntryOrder:
        if self.type == "limit" and self.limit_price is None:
            raise ValueError("EntryOrder type=limit requires limit_price")
        if self.type == "stop_limit" and (self.limit_price is None or self.stop_price is None):
            raise ValueError("EntryOrder type=stop_limit requires both limit_price and stop_price")
        return self


class PositionSize(BaseModel):
    """Sizing in shares/contracts and dollars.

    Per parent decision (B): no ``sector`` field. Sector is a risk-side
    concept derived by the guardrail layer at intake — not carried on the
    command's wire format.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    quantity: float = Field(gt=0)
    dollar_value: float = Field(gt=0)
    premium_at_risk: float | None = Field(default=None, gt=0)


class Target(BaseModel):
    """Take-profit target with conditional shapes per ``target_type``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_type: TargetType
    price: float | None = Field(default=None, gt=0)
    pl_percentage: float | None = None
    pl_dollar: float | None = None
    order_type: Literal["market", "limit"]

    @model_validator(mode="after")
    def _validate_invariants(self) -> Target:
        if self.target_type == "absolute_price" and self.price is None:
            raise ValueError("Target target_type=absolute_price requires price")
        if self.target_type == "pl_percentage" and (
            self.pl_percentage is None or self.price is None
        ):
            raise ValueError(
                "Target target_type=pl_percentage requires both pl_percentage and price"
            )
        if self.target_type == "pl_dollar" and (self.pl_dollar is None or self.price is None):
            raise ValueError("Target target_type=pl_dollar requires both pl_dollar and price")
        return self


# ---------------------------------------------------------------------------
# Invalidation legs (discriminated on ``type``)
# ---------------------------------------------------------------------------


class PriceCondition(BaseModel):
    """Price-trigger condition: an underlying ticker, comparator, and price."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    underlying_trigger: str = Field(min_length=1)
    comparator: Comparator
    trigger_price: float = Field(gt=0)


class TimeCondition(BaseModel):
    """Time-trigger condition: a deadline timestamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deadline: datetime


class EventCondition(BaseModel):
    """Event-trigger condition: a qualitative event description."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_description: str = Field(min_length=1)


class BracketOrderParameters(BaseModel):
    """Order parameters for a bracket leg (price/time legs only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_type: BracketOrderType
    limit_price: float | None = Field(default=None, gt=0)


class PriceLeg(BaseModel):
    """Hard price-trigger invalidation leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["price"]
    is_hard: Literal[True]
    condition: PriceCondition
    order_parameters: BracketOrderParameters


class TimeLeg(BaseModel):
    """Hard time-trigger invalidation leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["time"]
    is_hard: Literal[True]
    condition: TimeCondition
    order_parameters: BracketOrderParameters


class EventLeg(BaseModel):
    """Soft event-trigger invalidation leg (no order_parameters)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["event"]
    is_hard: Literal[False]
    condition: EventCondition


InvalidationLeg = Annotated[
    PriceLeg | TimeLeg | EventLeg,
    Discriminator("type"),
]


# ---------------------------------------------------------------------------
# Thesis sub-records
# ---------------------------------------------------------------------------


class ThesisComponent(BaseModel):
    """One thesis component — entry / target / invalidation rationale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_type: ComponentType
    linked_leg: str = Field(min_length=1)
    instrument_reference: str = Field(min_length=1)
    narrative: str = Field(min_length=1)
    key_assumptions: tuple[str, ...]


class Thesis(BaseModel):
    """Self-contained trade thesis — a summary plus per-leg components."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1)
    components: tuple[ThesisComponent, ...] = Field(min_length=1)


# ---------------------------------------------------------------------------
# ADJUST helpers
# ---------------------------------------------------------------------------


class NewStopLevel(BaseModel):
    """ADJUST replacement stop level."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_price: float = Field(gt=0)
    order_type: BracketOrderType
    limit_price: float | None = Field(default=None, gt=0)


class NewTargetLevel(BaseModel):
    """ADJUST replacement take-profit target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_type: TargetType | None = None
    price: float | None = Field(default=None, gt=0)
    pl_percentage: float | None = None
    pl_dollar: float | None = None
    order_type: Literal["market", "limit"]


class NewEventInvalidation(BaseModel):
    """ADJUST replacement event invalidation leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_description: str = Field(min_length=1)


class BracketAdjustment(BaseModel):
    """ADD-time optional bracket revision — same shape as ADJUST's change-fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_stop_level: NewStopLevel | None = None
    new_target_level: NewTargetLevel | None = None
    new_time_expiration: datetime | None = None

    @model_validator(mode="after")
    def _validate_at_least_one(self) -> BracketAdjustment:
        if (
            self.new_stop_level is None
            and self.new_target_level is None
            and self.new_time_expiration is None
        ):
            raise ValueError(
                "BracketAdjustment requires at least one of new_stop_level, "
                "new_target_level, or new_time_expiration"
            )
        return self


# ---------------------------------------------------------------------------
# command_id format documentation
# ---------------------------------------------------------------------------


_COMMAND_ID_DESCRIPTION = (
    "Assigned by the OMS command intake layer at envelope receipt. PM-originated "
    "ID format: ``^inv-[^.]+\\.ENV-(REC|SA|SA-ORD)-[0-9]+\\.[0-9]+\\.[0-9]+$``. "
    "Engine-originated ID format: ``^MON\\.[^.]+\\.[0-9]+\\.[0-9]+$``. "
    "Field-level pattern enforcement is deferred to the OMS intake layer (story 03); "
    "the LLM produces envelopes with command_id=None and the OMS fills it."
)


# ---------------------------------------------------------------------------
# Five command variants (discriminated on ``command_type``)
# ---------------------------------------------------------------------------


class OpenCommand(BaseModel):
    """OPEN command — new position with full bracket and thesis coverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str | None = Field(default=None, description=_COMMAND_ID_DESCRIPTION)
    command_type: Literal["open"]
    instrument: Instrument
    entry_order: EntryOrder
    position_size: PositionSize
    target: Target
    invalidation_legs: tuple[InvalidationLeg, ...] = Field(min_length=1)
    thesis: Thesis

    @model_validator(mode="after")
    def _validate_hard_backstop(self) -> OpenCommand:
        if not any(leg.is_hard for leg in self.invalidation_legs):
            raise ValueError(
                "OpenCommand invalidation_legs requires at least one is_hard=True leg "
                "(price- or time-based hard backstop)"
            )
        return self


class CloseCommand(BaseModel):
    """CLOSE command — exit a held position."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str | None = Field(default=None, description=_COMMAND_ID_DESCRIPTION)
    command_type: Literal["close"]
    position_id: str = Field(min_length=1)
    quantity: float | Literal["all"]
    order_type: Literal["market", "limit"]
    limit_price: float | None = Field(default=None, gt=0)
    close_rationale_type: CloseRationaleType
    invalidation_reason: str | None = None
    risk_management_subtype: RiskManagementSubtype | None = None

    @model_validator(mode="after")
    def _validate_invariants(self) -> CloseCommand:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("CloseCommand order_type=limit requires limit_price")
        if self.close_rationale_type == "thesis_invalidated" and self.invalidation_reason is None:
            raise ValueError(
                "CloseCommand close_rationale_type=thesis_invalidated requires invalidation_reason"
            )
        if self.close_rationale_type == "risk_management" and self.risk_management_subtype is None:
            raise ValueError(
                "CloseCommand close_rationale_type=risk_management requires risk_management_subtype"
            )
        if self.close_rationale_type == "conviction_reduced" and self.quantity == "all":
            raise ValueError(
                'CloseCommand close_rationale_type=conviction_reduced forbids quantity="all" '
                "(partial-close only)"
            )
        if self.quantity != "all" and self.quantity <= 0:
            raise ValueError('CloseCommand quantity must be > 0 or "all"')
        return self


class AdjustCommand(BaseModel):
    """ADJUST command — modify an open position's bracket or thesis components."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str | None = Field(default=None, description=_COMMAND_ID_DESCRIPTION)
    command_type: Literal["adjust"]
    position_id: str = Field(min_length=1)
    adjustment_rationale: str = Field(min_length=1)
    new_stop_level: NewStopLevel | None = None
    new_target_level: NewTargetLevel | None = None
    new_time_expiration: datetime | None = None
    new_event_invalidation: NewEventInvalidation | None = None
    thesis_component_updates: tuple[ThesisComponent, ...] | None = None

    @model_validator(mode="after")
    def _validate_at_least_one_change(self) -> AdjustCommand:
        if (
            self.new_stop_level is None
            and self.new_target_level is None
            and self.new_time_expiration is None
            and self.new_event_invalidation is None
            and self.thesis_component_updates is None
        ):
            raise ValueError(
                "AdjustCommand requires at least one of new_stop_level, new_target_level, "
                "new_time_expiration, new_event_invalidation, or thesis_component_updates"
            )
        return self


class CancelCommand(BaseModel):
    """CANCEL command — withdraw a pending order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str | None = Field(default=None, description=_COMMAND_ID_DESCRIPTION)
    command_type: Literal["cancel"]
    order_id: str = Field(min_length=1)
    cancel_reason: str = Field(min_length=1)


class AddCommand(BaseModel):
    """ADD command — increase exposure of an open position.

    The design's ``add_command.thesis_addition_component`` ``allOf`` constrains
    the appended component's ``component_type`` to ``entry_rationale``;
    enforced here via :func:`model_validator`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str | None = Field(default=None, description=_COMMAND_ID_DESCRIPTION)
    command_type: Literal["add"]
    position_id: str = Field(min_length=1)
    additional_quantity: float = Field(gt=0)
    additional_dollar_value: float = Field(gt=0)
    entry_order: EntryOrder
    thesis_addition_component: ThesisComponent
    bracket_adjustment: BracketAdjustment | None = None

    @model_validator(mode="after")
    def _validate_thesis_addition_component_type(self) -> AddCommand:
        if self.thesis_addition_component.component_type != "entry_rationale":
            raise ValueError(
                "AddCommand thesis_addition_component.component_type must be "
                '"entry_rationale" (the appended component is a new entry rationale)'
            )
        return self


# ---------------------------------------------------------------------------
# Discriminated union over the five command variants
# ---------------------------------------------------------------------------


OMSCommand = Annotated[
    OpenCommand | CloseCommand | AdjustCommand | CancelCommand | AddCommand,
    Discriminator("command_type"),
]


# ---------------------------------------------------------------------------
# Schema export accessor
# ---------------------------------------------------------------------------


_OMS_COMMAND_ADAPTER: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)


def oms_command_schema() -> dict[str, Any]:
    """Return the JSON Schema for the OMS command discriminated union."""
    schema = dict(_OMS_COMMAND_ADAPTER.json_schema())
    schema["title"] = "OMSCommand"
    return schema
