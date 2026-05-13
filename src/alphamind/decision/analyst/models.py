"""AnalystOutput data model — ALP-293.

Typed in-memory representation of the analyst agent's output document. The
parser (ALP-295) coerces SDK ``structured_output`` payloads to
:class:`AnalystOutput`; the validator (ALP-296) consumes it for cross-field
checks. ``AnalystOutput.model_json_schema()`` is fed to the SDK's
``output_format={"type": "json_schema", ...}`` mode so the API enforces the
shape post-generation.

The schema lives at ``docs/design/04-decision-layer/analyst-output-schema.md``
and is the authoritative contract; this module is the typed code-side
expression of that schema. Cross-field invariants the schema encodes via
``allOf`` blocks are mirrored here as ``model_validator(mode="after")``
checks.

The risk-side 4-way sector taxonomy (``tech | semis | financials | energy``)
is used here, NOT the analysis-side 3-way (``tech_semis | financials |
energy``) — see ALP-115 § Architectural invariants.

``RuleProjection`` and ``Greeks`` are imported from
:mod:`alphamind.risk_guardrails.guardrail_evaluation` rather than redefined.
Both are frozen dataclasses; ``GuardrailValidationResult`` carries them via
``arbitrary_types_allowed=True``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    field_validator,
    model_validator,
)

from alphamind._kernel.ids import (
    InvocationId,
    RecommendationId,
    Symbol,
)
from alphamind._kernel.ids import (
    recommendation_id as _recommendation_id_constructor,
)
from alphamind.risk_guardrails.guardrail_evaluation import Greeks, RuleProjection

__all__ = [
    "AnalystOutput",
    "EntryOrder",
    "EntryWindow",
    "EventCondition",
    "Greeks",
    "GuardrailValidationResult",
    "Instrument",
    "InstrumentEquity",
    "InstrumentOption",
    "InstrumentStrategy",
    "InvalidationCondition",
    "InvalidationLeg",
    "InvalidationRationale",
    "OrderParameters",
    "PositionSize",
    "PriceCondition",
    "Recommendation",
    "RuleProjection",
    "StrategyLeg",
    "Target",
    "TimeCondition",
    "WatchlistEntry",
]


# ---------------------------------------------------------------------------
# Sector taxonomy — risk-side 4-way (config/profiles/<profile>.yaml#active_sectors)
# ---------------------------------------------------------------------------

Sector = Literal["tech", "semis", "financials", "energy"]


# ---------------------------------------------------------------------------
# Instrument variants (discriminated union on ``asset_type``)
# ---------------------------------------------------------------------------


class InstrumentEquity(BaseModel):
    """Equity instrument variant."""

    model_config = ConfigDict(frozen=True)

    asset_type: Literal["equity"]
    ticker: Symbol = Field(min_length=1)
    direction: Literal["long", "short"]


class InstrumentOption(BaseModel):
    """Single-leg option instrument variant."""

    model_config = ConfigDict(frozen=True)

    asset_type: Literal["option"]
    underlying: Symbol = Field(min_length=1)
    strike: float = Field(gt=0)
    expiration: date
    contract_type: Literal["call", "put"]
    direction: Literal["long", "short"]


class StrategyLeg(BaseModel):
    """One leg of a multi-leg strategy."""

    model_config = ConfigDict(frozen=True)

    strike: float = Field(gt=0)
    expiration: date
    contract_type: Literal["call", "put"]
    direction: Literal["long", "short"]
    quantity_ratio: int = Field(ge=1)


class InstrumentStrategy(BaseModel):
    """Multi-leg strategy instrument variant."""

    model_config = ConfigDict(frozen=True)

    asset_type: Literal["strategy"]
    strategy_type: Literal[
        "vertical_spread",
        "calendar_spread",
        "straddle",
        "strangle",
        "iron_condor",
        "custom",
    ]
    underlying: Symbol = Field(min_length=1)
    legs: tuple[StrategyLeg, ...] = Field(min_length=2)


Instrument = Annotated[
    InstrumentEquity | InstrumentOption | InstrumentStrategy,
    Discriminator("asset_type"),
]


# ---------------------------------------------------------------------------
# EntryOrder
# ---------------------------------------------------------------------------


class EntryOrder(BaseModel):
    """Entry-order shape with conditional price-field invariants."""

    model_config = ConfigDict(frozen=True)

    type: Literal["market", "limit", "stop_limit"]
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_price_fields(self) -> EntryOrder:
        if self.type == "limit" and self.limit_price is None:
            raise ValueError("entry_order.type=limit requires limit_price")
        if self.type == "stop_limit" and (self.limit_price is None or self.stop_price is None):
            raise ValueError("entry_order.type=stop_limit requires both limit_price and stop_price")
        return self


# ---------------------------------------------------------------------------
# PositionSize
# ---------------------------------------------------------------------------


class PositionSize(BaseModel):
    """Per-recommendation sizing block.

    ``delta_adjusted_exposure`` is populated by the guardrail validation tool;
    the LLM mirrors the value verbatim into the recommendation. Can be
    negative for shorts. ``premium_at_risk`` is required for defined-risk
    options/strategies (the schema description lists it as required for that
    case but does not encode it as an invariant; that check lives in the
    Layer-2/3 validator, not here).
    """

    model_config = ConfigDict(frozen=True)

    quantity: float = Field(gt=0)
    dollar_value: float = Field(gt=0)
    pct_of_portfolio: float = Field(gt=0)
    premium_at_risk: float | None = Field(default=None, gt=0)
    delta_adjusted_exposure: float | None = None


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


class Target(BaseModel):
    """Exit target with conditional pl-extras invariants."""

    model_config = ConfigDict(frozen=True)

    target_type: Literal["absolute_price", "pl_percentage", "pl_dollar"]
    price: float = Field(gt=0)
    dollar_pl_target: float
    pl_percentage: float | None = None
    pl_dollar: float | None = None

    @model_validator(mode="after")
    def _validate_pl_fields(self) -> Target:
        if self.target_type == "pl_percentage" and self.pl_percentage is None:
            raise ValueError("target.target_type=pl_percentage requires pl_percentage")
        if self.target_type == "pl_dollar" and self.pl_dollar is None:
            raise ValueError("target.target_type=pl_dollar requires pl_dollar")
        return self


# ---------------------------------------------------------------------------
# Invalidation condition variants
# ---------------------------------------------------------------------------


class PriceCondition(BaseModel):
    """Price-trigger condition for a price invalidation leg."""

    model_config = ConfigDict(frozen=True)

    underlying_trigger: Symbol = Field(min_length=1)
    comparator: Literal["<=", ">=", "<", ">"]
    trigger_price: float = Field(gt=0)


class TimeCondition(BaseModel):
    """Deadline condition for a time invalidation leg."""

    model_config = ConfigDict(frozen=True)

    deadline: datetime

    @model_validator(mode="after")
    def _deadline_tz_aware(self) -> TimeCondition:
        if self.deadline.tzinfo is None:
            raise ValueError("TimeCondition.deadline must be tz-aware")
        return self


class EventCondition(BaseModel):
    """Qualitative-event condition for an event invalidation leg."""

    model_config = ConfigDict(frozen=True)

    event_description: str = Field(min_length=1)


InvalidationCondition = PriceCondition | TimeCondition | EventCondition


class OrderParameters(BaseModel):
    """Order-parameters block attached to price/time invalidation legs."""

    model_config = ConfigDict(frozen=True)

    order_type: Literal["market", "limit", "stop", "stop_limit"]
    limit_price: float | None = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# InvalidationLeg
# ---------------------------------------------------------------------------


class InvalidationLeg(BaseModel):
    """Single invalidation leg.

    Carries three coupled fields — ``type``, ``is_hard``, ``order_parameters``
    — bound by the schema's ``allOf`` block:

    * ``type=price``  ⇒ ``is_hard=True``,  ``order_parameters`` required,
      ``condition`` = :class:`PriceCondition`.
    * ``type=time``   ⇒ ``is_hard=True``,  ``order_parameters`` required,
      ``condition`` = :class:`TimeCondition`.
    * ``type=event``  ⇒ ``is_hard=False``, ``order_parameters`` omitted,
      ``condition`` = :class:`EventCondition`.
    """

    model_config = ConfigDict(frozen=True)

    leg_id: str = Field(pattern=r"^INV-[0-9]+$")
    type: Literal["price", "time", "event"]
    is_hard: bool
    condition: InvalidationCondition
    order_parameters: OrderParameters | None = None

    @model_validator(mode="after")
    def _validate_leg_invariant(self) -> InvalidationLeg:
        # Per-type expected shape: (must-be-hard, requires-order-parameters, condition-class).
        expected: dict[str, tuple[bool, bool, type]] = {
            "price": (True, True, PriceCondition),
            "time": (True, True, TimeCondition),
            "event": (False, False, EventCondition),
        }
        want_hard, wants_order_params, condition_cls = expected[self.type]
        if self.is_hard != want_hard:
            raise ValueError(f"invalidation_leg.type={self.type} requires is_hard={want_hard}")
        has_order_params = self.order_parameters is not None
        if has_order_params != wants_order_params:
            verb = "requires" if wants_order_params else "forbids"
            raise ValueError(f"invalidation_leg.type={self.type} {verb} order_parameters")
        if not isinstance(self.condition, condition_cls):
            # Pydantic's ValidationError aggregates ValueError, not TypeError, so
            # TRY004's "use TypeError for invalid type" guidance does not apply
            # to model_validator branches.
            raise ValueError(  # noqa: TRY004
                f"invalidation_leg.type={self.type} requires condition to be "
                f"{condition_cls.__name__}"
            )
        return self


# ---------------------------------------------------------------------------
# Other small records
# ---------------------------------------------------------------------------


class EntryWindow(BaseModel):
    """Optional entry-window block."""

    model_config = ConfigDict(frozen=True)

    deadline: datetime
    decay_type: Literal["binary", "gradual"]
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _deadline_tz_aware(self) -> EntryWindow:
        if self.deadline.tzinfo is None:
            raise ValueError("EntryWindow.deadline must be tz-aware")
        return self


class InvalidationRationale(BaseModel):
    """Per-leg invalidation rationale.

    The cross-field check that every ``leg_id`` resolves to an existing
    ``invalidation_legs[].leg_id`` is the validator's responsibility (Pydantic
    cannot see across siblings).
    """

    model_config = ConfigDict(frozen=True)

    leg_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class GuardrailValidationResult(BaseModel):
    """Mirror of the validation tool's :class:`ValidationResult` shape.

    Reuses :class:`RuleProjection` and :class:`Greeks` from
    :mod:`alphamind.risk_guardrails.guardrail_evaluation` (frozen dataclasses,
    not Pydantic models — ``arbitrary_types_allowed=True`` carries them).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    overall: Literal["PASS", "FAIL"]
    per_rule: tuple[RuleProjection, ...]
    delta_adjusted_exposure: float | None = None
    greeks: Greeks | None = None
    cumulative_impact_note: str | None = None
    checked_at: datetime

    @model_validator(mode="after")
    def _checked_at_tz_aware(self) -> GuardrailValidationResult:
        if self.checked_at.tzinfo is None:
            raise ValueError("GuardrailValidationResult.checked_at must be tz-aware")
        return self


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


class Recommendation(BaseModel):
    """One analyst trade recommendation."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    recommendation_id: RecommendationId = Field(pattern=r"^REC-[0-9]+$")
    instrument: Instrument
    underlying: Symbol = Field(min_length=1)
    sector: Sector
    conviction_level: int = Field(ge=1, le=5)
    entry_order: EntryOrder
    position_size: PositionSize
    target: Target
    invalidation_legs: tuple[InvalidationLeg, ...] = Field(min_length=1)
    entry_window: EntryWindow | None = None
    time_expectation_hours: float = Field(gt=0, le=72)
    guardrail_validation_result: GuardrailValidationResult
    thesis_narrative: str = Field(min_length=1)
    target_rationale: str = Field(min_length=1)
    invalidation_rationale: tuple[InvalidationRationale, ...] = Field(min_length=1)
    position_size_rationale: str = Field(min_length=1)
    entry_window_rationale: str | None = None
    counterarguments_acknowledged: str = Field(min_length=1)

    @field_validator("recommendation_id", mode="after")
    @classmethod
    def _construct_recommendation_id(cls, value: str) -> RecommendationId:
        return _recommendation_id_constructor(value)

    @model_validator(mode="after")
    def _validate_entry_window_pairing(self) -> Recommendation:
        if (self.entry_window is None) != (self.entry_window_rationale is None):
            raise ValueError(
                "entry_window and entry_window_rationale must be paired "
                "(both present or both absent)"
            )
        return self


# ---------------------------------------------------------------------------
# WatchlistEntry
# ---------------------------------------------------------------------------


class WatchlistEntry(BaseModel):
    """Lighter-weight watchlist entry emitted under halt mode."""

    model_config = ConfigDict(frozen=True)

    ticker: Symbol = Field(min_length=1)
    sector: Sector
    thesis_summary: str = Field(min_length=1)
    estimated_conviction: int = Field(ge=1, le=5)
    source_references: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# AnalystOutput root
# ---------------------------------------------------------------------------


class AnalystOutput(BaseModel):
    """Single analyst invocation's output.

    Mode-conditional invariant: when ``mode="normal"``, ``recommendations`` is
    required (may be empty tuple) and ``watchlist`` must be ``None``. When
    ``mode="watchlist"``, ``watchlist`` is required (may be empty tuple) and
    ``recommendations`` must be ``None``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    invocation_id: InvocationId = Field(min_length=1)
    timestamp: datetime
    mode: Literal["normal", "watchlist"]
    recommendations: tuple[Recommendation, ...] | None = None
    watchlist: tuple[WatchlistEntry, ...] | None = None

    @model_validator(mode="after")
    def _validate_invariants(self) -> AnalystOutput:
        if self.timestamp.tzinfo is None:
            raise ValueError("AnalystOutput.timestamp must be tz-aware")
        if self.mode == "normal":
            if self.recommendations is None:
                raise ValueError("mode=normal requires recommendations")
            if self.watchlist is not None:
                raise ValueError("mode=normal forbids watchlist")
        elif self.mode == "watchlist":
            if self.watchlist is None:
                raise ValueError("mode=watchlist requires watchlist")
            if self.recommendations is not None:
                raise ValueError("mode=watchlist forbids recommendations")
        return self
