"""Consumer-facing typed records for orders and brackets (story 03c)."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    CommandId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind.portfolio_state.records.positions import InstrumentType, OptionContractType


class OrderRole(StrEnum):
    ENTRY = "ENTRY"
    TAKE_PROFIT = "TAKE_PROFIT"
    PRICE_STOP = "PRICE_STOP"
    TIME_STOP = "TIME_STOP"
    CLOSE = "CLOSE"
    ADD_ENTRY = "ADD_ENTRY"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_OPEN = "SELL_TO_OPEN"
    BUY_TO_CLOSE = "BUY_TO_CLOSE"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"


class OrderClass(StrEnum):
    """Contingent-order class per orders-and-brackets.md § Tier 2.

    SIMPLE — atomic single order; no contingent linkage. Default.
    BRACKET — entry + take-profit + stop, submitted as a unit linked to a thesis.
    OCO — one-cancels-other; pair of orders where filling one cancels the other.
    OTO — one-triggers-other; activates contingent orders only on its own fill.
    MLEG — multi-leg options strategy submitted as a single Alpaca mleg order;
           requires the instrument_spec to have instrument_type=STRATEGY.
    """

    SIMPLE = "SIMPLE"
    BRACKET = "BRACKET"
    OCO = "OCO"
    OTO = "OTO"
    MLEG = "MLEG"


class OrderDuration(StrEnum):
    """Order time-in-force duration for AlphaMind's 4-72h trading horizons.

    DAY — expires at end of the current trading session.
    GTC — good-till-cancelled; remains active until filled or explicitly cancelled.
    GTD — good-till-date; expires at the end of a specified session date.
    """

    DAY = "DAY"
    GTC = "GTC"
    GTD = "GTD"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class BracketStatus(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    DISSOLVED = "DISSOLVED"


class BracketLegType(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    PRICE_STOP = "PRICE_STOP"
    TIME_EXPIRATION = "TIME_EXPIRATION"
    EVENT_INVALIDATION = "EVENT_INVALIDATION"


class BracketLegEnforcement(StrEnum):
    MECHANICAL = "MECHANICAL"
    ADVISORY = "ADVISORY"


class BracketLegStatus(StrEnum):
    PENDING_ACTIVATION = "PENDING_ACTIVATION"
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    CANCELLED = "CANCELLED"


# Mechanical leg types that satisfy the hard-backstop requirement
_MECHANICAL_BACKSTOP_TYPES = frozenset(
    {BracketLegType.TAKE_PROFIT, BracketLegType.PRICE_STOP, BracketLegType.TIME_EXPIRATION}
)


class EquityInstrumentSpec(BaseModel):
    """Instrument spec for an equity order, discriminated by ``instrument_type=EQUITY``."""

    model_config = ConfigDict(frozen=True)

    instrument_type: Literal[InstrumentType.EQUITY] = InstrumentType.EQUITY
    ticker: Symbol = Field(min_length=1)


class OptionsInstrumentSpec(BaseModel):
    """Instrument spec for an options order, discriminated by ``instrument_type=OPTIONS``."""

    model_config = ConfigDict(frozen=True)

    instrument_type: Literal[InstrumentType.OPTIONS] = InstrumentType.OPTIONS
    underlying: Symbol = Field(min_length=1)
    strike: float
    expiration: date
    contract_type: OptionContractType
    contract_multiplier: float


class StrategyInstrumentSpec(BaseModel):
    """Instrument spec for a multi-leg strategy order.

    Legs are tightened from the prior generic ``InstrumentSpec`` to
    ``OptionsInstrumentSpec`` — strategy legs are always options.
    """

    model_config = ConfigDict(frozen=True)

    instrument_type: Literal[InstrumentType.STRATEGY] = InstrumentType.STRATEGY
    legs: tuple[OptionsInstrumentSpec, ...]

    @model_validator(mode="after")
    def _legs_non_empty(self) -> StrategyInstrumentSpec:
        if not self.legs:
            msg = "STRATEGY InstrumentSpec requires non-empty legs"
            raise ValueError(msg)
        return self


InstrumentSpec = Annotated[
    EquityInstrumentSpec | OptionsInstrumentSpec | StrategyInstrumentSpec,
    Field(discriminator="instrument_type"),
]


class PriceParameters(BaseModel):
    """Price parameters for an order; cross-validation is enforced at OrderRecord level."""

    model_config = ConfigDict(frozen=True)

    limit_price: float | None = None
    stop_trigger_price: float | None = None


class OrderRecord(BaseModel):
    """Consumer-facing per-order record."""

    model_config = ConfigDict(frozen=True)

    order_id: OrderId
    position_id: PositionId | None
    bracket_id: BracketId
    role: OrderRole
    instrument_spec: InstrumentSpec
    direction: OrderDirection
    order_type: OrderType
    order_class: OrderClass = OrderClass.SIMPLE
    price_parameters: PriceParameters
    quantity: float
    duration: OrderDuration
    status: OrderStatus
    alpaca_order_id: AlpacaOrderId
    alpaca_order_id_chain: tuple[AlpacaOrderId, ...]
    submission_timestamp: datetime
    last_update_timestamp: datetime
    filled_quantity: float
    avg_fill_price: float | None
    remaining_quantity: float
    modification_count: int
    originating_thesis_id: ThesisId | None
    originating_pm_command_id: CommandId | None
    age_hours: float

    @model_validator(mode="after")
    def _validate_all(self) -> OrderRecord:
        self._check_mleg_requires_strategy()
        self._check_price_parameters()
        self._check_quantity_constraints()
        self._check_alpaca_chain()
        self._check_age_hours()
        return self

    def _check_mleg_requires_strategy(self) -> None:
        if self.order_class != OrderClass.MLEG:
            return
        if self.instrument_spec.instrument_type != InstrumentType.STRATEGY:
            msg = (
                f"order_class=MLEG requires instrument_spec.instrument_type=STRATEGY; "
                f"got {self.instrument_spec.instrument_type!r}"
            )
            raise ValueError(msg)

    def _check_price_parameters(self) -> None:
        pp = self.price_parameters
        ot = self.order_type
        if ot == OrderType.MARKET:
            self._check_market_prices(pp)
        elif ot == OrderType.LIMIT:
            self._check_limit_prices(pp)
        elif ot == OrderType.STOP:
            self._check_stop_prices(pp)
        elif ot == OrderType.STOP_LIMIT:
            self._check_stop_limit_prices(pp)

    @staticmethod
    def _check_market_prices(pp: PriceParameters) -> None:
        if pp.limit_price is not None or pp.stop_trigger_price is not None:
            msg = "MARKET order must have both limit_price and stop_trigger_price as None"
            raise ValueError(msg)

    @staticmethod
    def _check_limit_prices(pp: PriceParameters) -> None:
        if pp.limit_price is None:
            msg = "LIMIT order requires limit_price"
            raise ValueError(msg)
        if pp.stop_trigger_price is not None:
            msg = "LIMIT order must have stop_trigger_price as None"
            raise ValueError(msg)

    @staticmethod
    def _check_stop_prices(pp: PriceParameters) -> None:
        if pp.stop_trigger_price is None:
            msg = "STOP order requires stop_trigger_price"
            raise ValueError(msg)
        if pp.limit_price is not None:
            msg = "STOP order must have limit_price as None"
            raise ValueError(msg)

    @staticmethod
    def _check_stop_limit_prices(pp: PriceParameters) -> None:
        if pp.limit_price is None or pp.stop_trigger_price is None:
            msg = "STOP_LIMIT order requires both limit_price and stop_trigger_price"
            raise ValueError(msg)

    def _check_quantity_constraints(self) -> None:
        if self.quantity <= 0:
            msg = f"quantity must be > 0; got {self.quantity}"
            raise ValueError(msg)
        if self.filled_quantity < 0:
            msg = f"filled_quantity must be >= 0; got {self.filled_quantity}"
            raise ValueError(msg)
        if self.remaining_quantity < 0:
            msg = f"remaining_quantity must be >= 0; got {self.remaining_quantity}"
            raise ValueError(msg)
        if self.modification_count < 0:
            msg = f"modification_count must be >= 0; got {self.modification_count}"
            raise ValueError(msg)

        if self.status == OrderStatus.REJECTED:
            if self.filled_quantity != 0:
                msg = "REJECTED order must have filled_quantity == 0"
                raise ValueError(msg)
            if self.remaining_quantity != self.quantity:
                msg = "REJECTED order must have remaining_quantity == quantity"
                raise ValueError(msg)
        else:
            if self.filled_quantity + self.remaining_quantity != self.quantity:
                msg = (
                    f"filled_quantity ({self.filled_quantity}) + remaining_quantity "
                    f"({self.remaining_quantity}) must equal quantity ({self.quantity})"
                )
                raise ValueError(msg)

    def _check_alpaca_chain(self) -> None:
        if not self.alpaca_order_id_chain:
            msg = "alpaca_order_id_chain must be non-empty"
            raise ValueError(msg)
        if self.alpaca_order_id != self.alpaca_order_id_chain[-1]:
            msg = (
                f"alpaca_order_id ({self.alpaca_order_id!r}) must equal "
                f"alpaca_order_id_chain[-1] ({self.alpaca_order_id_chain[-1]!r})"
            )
            raise ValueError(msg)

    def _check_age_hours(self) -> None:
        if self.age_hours < 0:
            msg = f"age_hours must be >= 0; got {self.age_hours}"
            raise ValueError(msg)


class BracketLegModification(BaseModel):
    """One entry per modification event in a bracket's modification history."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    pm_command_id: CommandId | None
    source: str
    field_changed: str
    old_value: str
    new_value: str
    rationale: str


class PriceTrigger(BaseModel):
    """Trigger for TAKE_PROFIT and PRICE_STOP legs.

    Evaluates against the underlying equity's real-time price stream
    (per orders-and-brackets.md § Options price-based stops). For equity
    positions, ``underlying_ticker`` is the equity itself; for options
    positions, it is the option's underlying equity.
    """

    model_config = ConfigDict(frozen=True)

    trigger_type: Literal["price"] = "price"
    underlying_ticker: Symbol = Field(min_length=1)
    threshold_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    direction: Literal["GTE", "LTE"]


class TimeTrigger(BaseModel):
    """Trigger for TIME_EXPIRATION legs.

    Fires when wall-clock time crosses the deadline.
    """

    model_config = ConfigDict(frozen=True)

    trigger_type: Literal["time"] = "time"
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "deadline must be tz-aware UTC"
            raise ValueError(msg)
        return v


class EventTrigger(BaseModel):
    """Trigger for EVENT_INVALIDATION legs.

    Qualitative condition the analysis pipeline evaluates. The engine
    cannot enforce mechanically; the PM acts on the flag.
    """

    model_config = ConfigDict(frozen=True)

    trigger_type: Literal["event"] = "event"
    description: str = Field(min_length=1)
    condition_evaluator_id: str | None = None


TriggerPayload = Annotated[
    PriceTrigger | TimeTrigger | EventTrigger,
    Field(discriminator="trigger_type"),
]


class PLAnchorSpec(BaseModel):
    """P/L-anchor specification for legs defined in P/L-percentage terms.

    Per orders-and-brackets.md § P/L-based bracket legs:

    * At OPEN time, the producer specifies one of `target_pct` or `stop_pct`
      (per leg type) plus the `planned_entry_price` (the entry order's planned
      fill price). The engine submits the equivalent absolute price to the
      broker.
    * On entry fill, the engine recalculates the absolute price using
      `actual_entry_price` as the anchor. The recalculation logs as a
      `bracket_modified` event with source `FILL_ANCHOR_RECALCULATION`. Once
      complete, `recalculated_at_fill` is True.

    Sign conventions:

    * target_pct: percentage gain (e.g., 0.80 = 80% profit on premium for an
      options take-profit; 0.05 = 5% gain for an equity take-profit).
    * stop_pct: percentage loss (e.g., 0.30 = 30% loss on premium; 0.05 = 5%
      loss on equity). Always expressed as a positive magnitude.
    """

    model_config = ConfigDict(frozen=True)

    spec_type: Literal["target", "stop"]
    pct: Annotated[float, Field(gt=0, le=10.0, allow_inf_nan=False)]
    planned_entry_price: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    actual_entry_price: float | None = None
    recalculated_at_fill: bool = False

    @model_validator(mode="after")
    def _validate_recalc_consistency(self) -> PLAnchorSpec:
        if self.recalculated_at_fill and self.actual_entry_price is None:
            msg = "recalculated_at_fill=True requires actual_entry_price to be non-None"
            raise ValueError(msg)
        if not self.recalculated_at_fill and self.actual_entry_price is not None:
            msg = "actual_entry_price must be None when recalculated_at_fill=False"
            raise ValueError(msg)
        return self


_LEG_TYPE_TO_TRIGGER_TYPE: dict[BracketLegType, str] = {
    BracketLegType.TAKE_PROFIT: "price",
    BracketLegType.PRICE_STOP: "price",
    BracketLegType.TIME_EXPIRATION: "time",
    BracketLegType.EVENT_INVALIDATION: "event",
}

_LEG_TYPE_TO_PL_ANCHOR_SPEC_TYPE: dict[BracketLegType, str] = {
    BracketLegType.TAKE_PROFIT: "target",
    BracketLegType.PRICE_STOP: "stop",
}


class BracketLeg(BaseModel):
    """One leg definition within a bracket's protective set."""

    model_config = ConfigDict(frozen=True)

    leg_id: str
    leg_type: BracketLegType
    order_id: OrderId | None
    trigger: TriggerPayload
    enforcement: BracketLegEnforcement
    status: BracketLegStatus
    pl_anchor: PLAnchorSpec | None = None

    @model_validator(mode="after")
    def _validate_event_invalidation(self) -> BracketLeg:
        if self.leg_type == BracketLegType.EVENT_INVALIDATION and self.order_id is not None:
            msg = "EVENT_INVALIDATION leg must have order_id as None"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_trigger_matches_leg_type(self) -> BracketLeg:
        expected = _LEG_TYPE_TO_TRIGGER_TYPE[self.leg_type]
        if self.trigger.trigger_type != expected:
            msg = (
                f"leg_type={self.leg_type!r} requires trigger_type={expected!r}; "
                f"got trigger_type={self.trigger.trigger_type!r}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_pl_anchor_compatibility(self) -> BracketLeg:
        if self.pl_anchor is None:
            return self
        expected_spec_type = _LEG_TYPE_TO_PL_ANCHOR_SPEC_TYPE.get(self.leg_type)
        if expected_spec_type is None:
            msg = (
                f"pl_anchor only valid on TAKE_PROFIT or PRICE_STOP legs; "
                f"got leg_type={self.leg_type!r}"
            )
            raise ValueError(msg)
        if self.pl_anchor.spec_type != expected_spec_type:
            msg = (
                f"leg_type={self.leg_type!r} requires pl_anchor.spec_type="
                f"{expected_spec_type!r}; got {self.pl_anchor.spec_type!r}"
            )
            raise ValueError(msg)
        return self


class BracketRecord(BaseModel):
    """Consumer-facing per-bracket record.

    Lifecycle semantics for ``entry_window_deadline``:

    * When ``entry_window_deadline is not None`` AND ``status == PENDING_ENTRY``: the
      entry order should auto-cancel if ``now() > entry_window_deadline`` and the entry
      hasn't filled. Enforcement is the OMS's responsibility (ALP-120), not the record's.
    * When ``status in {ACTIVE, COMPLETED, DISSOLVED}``: the field is informational only —
      the entry has already filled (ACTIVE) or the bracket has resolved
      (COMPLETED/DISSOLVED).

    Producers that do not know the entry window may leave the field ``None`` even on
    ``PENDING_ENTRY`` (graceful degradation; the analyst proposing the bracket is
    responsible for setting it).
    """

    model_config = ConfigDict(frozen=True)

    bracket_id: BracketId
    position_id: PositionId
    status: BracketStatus
    entry_order_id: OrderId
    protective_legs: tuple[BracketLeg, ...]
    modification_history: tuple[BracketLegModification, ...]
    corporate_action_cancellation_reason: str | None
    entry_window_deadline: datetime | None = None

    @field_validator("entry_window_deadline")
    @classmethod
    def _require_tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            msg = "entry_window_deadline must be tz-aware UTC when not None"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _validate_all(self) -> BracketRecord:
        self._check_protective_legs_non_empty()
        self._check_hard_backstop()
        self._check_pending_entry_rule()
        self._check_dissolved_rule()
        return self

    def _check_protective_legs_non_empty(self) -> None:
        if len(self.protective_legs) == 0:
            msg = "protective_legs must be non-empty (hard-backstop requirement)"
            raise ValueError(msg)

    def _check_hard_backstop(self) -> None:
        has_backstop = any(
            leg.enforcement == BracketLegEnforcement.MECHANICAL
            and leg.leg_type in _MECHANICAL_BACKSTOP_TYPES
            for leg in self.protective_legs
        )
        if not has_backstop:
            msg = (
                "BracketRecord must have at least one MECHANICAL leg with leg_type in "
                "{TAKE_PROFIT, PRICE_STOP, TIME_EXPIRATION} (hard-backstop requirement)"
            )
            raise ValueError(msg)

    def _check_pending_entry_rule(self) -> None:
        if self.status == BracketStatus.PENDING_ENTRY:
            self._require_all_legs_status(BracketLegStatus.PENDING_ACTIVATION, "PENDING_ENTRY")

    def _check_dissolved_rule(self) -> None:
        if self.status == BracketStatus.DISSOLVED:
            self._require_all_legs_status(BracketLegStatus.CANCELLED, "DISSOLVED")

    def _require_all_legs_status(
        self, required: BracketLegStatus, bracket_status_label: str
    ) -> None:
        bad = [leg.leg_id for leg in self.protective_legs if leg.status != required]
        if bad:
            msg = (
                f"{bracket_status_label} bracket requires all legs to have status "
                f"{required}; offending leg_ids: {bad}"
            )
            raise ValueError(msg)
