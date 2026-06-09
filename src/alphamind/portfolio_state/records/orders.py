"""Consumer-facing typed records for orders and brackets (story 03c)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    ClientOrderId,
    CommandId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import Price
from alphamind.portfolio_state.records.positions import InstrumentType, OptionContractType


class OrderRole(StrEnum):
    ENTRY = "ENTRY"
    TAKE_PROFIT = "TAKE_PROFIT"
    PRICE_STOP = "PRICE_STOP"
    TIME_STOP = "TIME_STOP"
    CLOSE = "CLOSE"
    ADD_ENTRY = "ADD_ENTRY"


# The protective-leg roles a bracket carries — the take-profit + the hard
# invalidation legs (price / time). Entry / add-entry are deliberately excluded
# (they are not protection). Single source of truth so the command-execution
# leg-cancellation sweep (``command_execution._shared``) and the decision-side
# close leg resolution (``submit_envelope.dispatch``) cannot drift — a drift
# would silently omit a protective leg from a CLOSE's pre-cancel, the exact
# ``held_for_orders`` defect ALP-937 fixes.
PROTECTIVE_LEG_ROLE_VALUES: frozenset[str] = frozenset(
    {OrderRole.PRICE_STOP.value, OrderRole.TAKE_PROFIT.value, OrderRole.TIME_STOP.value}
)


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


_DIRECTION_TO_SIDE: dict[OrderDirection, Literal["buy", "sell"]] = {
    OrderDirection.BUY: "buy",
    OrderDirection.BUY_TO_OPEN: "buy",
    OrderDirection.BUY_TO_CLOSE: "buy",
    OrderDirection.SELL: "sell",
    OrderDirection.SELL_TO_OPEN: "sell",
    OrderDirection.SELL_TO_CLOSE: "sell",
}


def direction_to_side(direction: OrderDirection) -> Literal["buy", "sell"]:
    """Collapse the six-variant ``OrderDirection`` to the buy-vs-sell partition.

    Consumed by execution-side cash-movement (debit on buy, credit on sell)
    and the paper-evaluation harness wedge (live-adjusted-price sign + fee
    dispatch). The open/close discriminator is intentionally dropped — both
    halves of the partition share the same downstream treatment. A new
    ``OrderDirection`` variant must be added to the mapping or this raises
    ``KeyError``.
    """
    return _DIRECTION_TO_SIDE[direction]


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
    # PENDING_SUBMIT (ALP-836) is the durable-intent state of an order whose row
    # has been committed locally but has NOT yet been accepted by the broker — the
    # atomicity-first window between the pre-dispatch commit and the post-submit
    # ``alpaca_order_id`` backfill. The row carries NO broker id (``alpaca_order_id``
    # NULL, ALP-847 — the synthetic ``alp-`` placeholder is deleted) until the real
    # broker id is backfilled, at which point it transitions to PENDING. A row
    # stuck in PENDING_SUBMIT means the broker never
    # accepted the order (lost backfill, rejection, or process death between the
    # pre-commit and dispatch) — recoverable by the reconcile-by-``client_order_id``
    # backfill, and never a live-broker-order-without-a-local-row strand.
    PENDING_SUBMIT = "PENDING_SUBMIT"
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


class EnforcementBinding(StrEnum):
    """How a protective leg's exit is *enforced* — a Broker-Owned Fact or Intent.

    The typed broker-vs-monitor distinction from ADR-0003. Orthogonal to
    ``BracketLegEnforcement`` (MECHANICAL/ADVISORY, the leg's semantic role):
    this names *who* enforces the leg.

    * ``BROKER_ENFORCED`` — backed by a real broker order (an equity native
      bracket/OTO child, or an options capital-protection ``stop_limit``); its
      execution is a Broker-Owned Fact and survives a monitor outage.
    * ``MONITOR_ENFORCED`` — armed Intent with no broker order; the continuous
      monitor watches the condition and submits a fresh self-attributing close
      when it fires. "Cancel" is a local Intent state change, not a broker call.
    """

    BROKER_ENFORCED = "broker_enforced"
    MONITOR_ENFORCED = "monitor_enforced"


class BracketLegStatus(StrEnum):
    PENDING_ACTIVATION = "PENDING_ACTIVATION"
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    CANCELLED = "CANCELLED"


class TriggerSignal(StrEnum):
    """Which signal a thesis-invalidation stop fires on (ALP-852 / ADR-0003).

    The persisted counterpart of the wire ``PriceLeg.trigger_signal`` tag,
    matched to the thesis nature: ``UNDERLYING_PRICE`` for a *directional* thesis
    (the underlying crosses an invalidating level); ``OPTION_PRICE`` for a
    *non-directional* single-option vol thesis (its own mark is the faithful
    signal); ``NET_MARK`` for a *non-directional* multi-leg spread (the strategy
    net mark). The continuous monitor reads this off the invalidation leg to
    select the trigger evaluator — an underlying-level trigger is meaningless
    for a spread whose PnL is nonlinear in the underlying.
    """

    UNDERLYING_PRICE = "underlying_price"
    OPTION_PRICE = "option_price"
    NET_MARK = "net_mark"


# Mechanical leg types that satisfy the hard-backstop requirement
_MECHANICAL_BACKSTOP_TYPES = frozenset(
    {BracketLegType.TAKE_PROFIT, BracketLegType.PRICE_STOP, BracketLegType.TIME_EXPIRATION}
)


@dataclass(frozen=True, slots=True)
class EquityInstrumentSpec:
    """Instrument spec for an equity order, discriminated by ``instrument_type=EQUITY``."""

    ticker: Symbol
    instrument_type: InstrumentType = field(default=InstrumentType.EQUITY, init=False)

    def __post_init__(self) -> None:
        if len(self.ticker) < 1:
            msg = "ticker must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class OptionsInstrumentSpec:
    """Instrument spec for an options order, discriminated by ``instrument_type=OPTIONS``."""

    underlying: Symbol
    strike: float
    expiration: date
    contract_type: OptionContractType
    contract_multiplier: float
    instrument_type: InstrumentType = field(default=InstrumentType.OPTIONS, init=False)

    def __post_init__(self) -> None:
        if len(self.underlying) < 1:
            msg = "underlying must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class StrategyInstrumentSpec:
    """Instrument spec for a multi-leg strategy order.

    Legs are tightened from the prior generic ``InstrumentSpec`` to
    ``OptionsInstrumentSpec`` — strategy legs are always options.
    """

    legs: tuple[OptionsInstrumentSpec, ...]
    instrument_type: InstrumentType = field(default=InstrumentType.STRATEGY, init=False)

    def __post_init__(self) -> None:
        if not self.legs:
            msg = "STRATEGY InstrumentSpec requires non-empty legs"
            raise ValueError(msg)


InstrumentSpec = EquityInstrumentSpec | OptionsInstrumentSpec | StrategyInstrumentSpec


@dataclass(frozen=True, slots=True)
class PriceParameters:
    """Price parameters for an order; cross-validation is enforced at OrderRecord level."""

    limit_price: Price | None = None
    stop_trigger_price: Price | None = None


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """Consumer-facing per-order record."""

    order_id: OrderId
    position_id: PositionId | None
    bracket_id: BracketId
    role: OrderRole
    instrument_spec: InstrumentSpec
    direction: OrderDirection | None
    order_type: OrderType
    price_parameters: PriceParameters
    quantity: float
    duration: OrderDuration
    status: OrderStatus
    # ADR-0003 / ALP-847: the broker's real Alpaca order id, or ``None`` for an
    # order with no broker counterpart. ``None`` covers two cases: a not-yet-
    # routed order (PENDING_SUBMIT — durable Intent keyed by ``client_order_id``,
    # backfilled with the real id on dispatch) and a monitor-enforced protective
    # leg (armed Intent the continuous monitor enforces — it never has a broker
    # order). The synthetic ``alp-{order_id}`` placeholder is deleted: a leg with
    # no broker order has NO broker id (invariant 5), never a counterfeit one.
    alpaca_order_id: AlpacaOrderId | None
    # Empty for an order with no broker id; otherwise the lineage of real broker
    # ids (cancel-and-replace ADJUST appends). The last element equals
    # ``alpaca_order_id`` when both are present.
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
    order_class: OrderClass = OrderClass.SIMPLE
    # ALP-836 — the broker ``client_order_id`` this order was (or will be)
    # submitted under. Equals the originating command_id for the primary order a
    # command produces (entry / close / add-entry / ADJUST replacement); ``None``
    # for native-bracket protective children (Alpaca generates their
    # client_order_ids) and for orders never dispatched under one. Indexed +
    # unique-when-present so a fill / reconcile pass can resolve the durable
    # pre-committed row by the ``client_order_id`` the broker fill carries, even
    # before the real ``alpaca_order_id`` has been backfilled.
    client_order_id: ClientOrderId | None = None

    def __post_init__(self) -> None:
        self._check_mleg_requires_strategy()
        self._check_direction_matches_order_class()
        self._check_price_parameters()
        self._check_quantity_constraints()
        self._check_alpaca_chain()
        self._check_age_hours()

    def _check_mleg_requires_strategy(self) -> None:
        is_mleg = self.order_class == OrderClass.MLEG
        is_strategy_spec = self.instrument_spec.instrument_type == InstrumentType.STRATEGY
        if is_mleg and not is_strategy_spec:
            msg = (
                f"order_class=MLEG requires instrument_spec.instrument_type=STRATEGY; "
                f"got {self.instrument_spec.instrument_type!r}"
            )
            raise ValueError(msg)
        if is_strategy_spec and not is_mleg:
            # Closes the converse the prior one-directional check left open:
            # a StrategyInstrumentSpec must ride on an MLEG envelope, so the
            # transitive direction=None ⇔ strategy spec invariant holds.
            msg = (
                f"instrument_spec.instrument_type=STRATEGY requires "
                f"order_class=MLEG; got {self.order_class!r}"
            )
            raise ValueError(msg)

    def _check_direction_matches_order_class(self) -> None:
        """``direction is None`` iff ``order_class == OrderClass.MLEG``.

        Order-level direction is a category error for a multi-leg strategy MLEG
        envelope — the broker adapter emits per-leg ``side`` /
        ``position_intent`` from each :class:`StrategyLeg` and the
        envelope-level field has no coherent single value. Every other order
        class carries a non-``None`` ``OrderDirection``. The companion
        ``_check_mleg_requires_strategy`` validator ties ``MLEG`` to a
        :class:`StrategyInstrumentSpec`, so transitively a strategy spec also
        implies ``direction is None``. Mirrors :class:`PositionRecord`'s
        direction-vs-payload binding (ALP-591); :func:`order_direction` is
        the canonical accessor. ALP-614.
        """
        is_mleg = self.order_class == OrderClass.MLEG
        if is_mleg and self.direction is not None:
            msg = "direction must be None for an MLEG strategy order"
            raise ValueError(msg)
        if not is_mleg and self.direction is None:
            msg = "direction must be non-None for a non-MLEG order"
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
        # ALP-847 — an order with no broker id (a not-yet-routed order or a
        # monitor-enforced protective leg) carries ``alpaca_order_id=None`` and an
        # empty chain; the two are bound together so a stray half-state can't form.
        if self.alpaca_order_id is None:
            if self.alpaca_order_id_chain:
                msg = "alpaca_order_id is None requires an empty alpaca_order_id_chain"
                raise ValueError(msg)
            return
        if not self.alpaca_order_id_chain:
            msg = "alpaca_order_id_chain must be non-empty when alpaca_order_id is set"
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


def order_direction(order: OrderRecord) -> OrderDirection | None:
    """Return the order-level direction, MLEG-aware.

    A simple / bracket / OCO / OTO order carries a meaningful single
    :class:`OrderDirection`; the accessor returns it. A multi-leg strategy
    MLEG envelope has no coherent envelope-level side — the broker adapter
    emits per-leg ``side`` / ``position_intent`` from each
    :class:`StrategyLeg` — and the validator ties ``direction is None`` to
    ``order_class == OrderClass.MLEG``, so the accessor returns ``None`` for
    a strategy order.

    This is the one accessor for order-level direction: consumers must not
    read ``OrderRecord.direction`` directly. Mirrors
    :func:`position_direction` on :class:`PositionRecord`. ALP-614.
    """
    return order.direction


@dataclass(frozen=True, slots=True)
class BracketLegModification:
    """One entry per modification event in a bracket's modification history."""

    timestamp: datetime
    pm_command_id: CommandId | None
    source: str
    field_changed: str
    old_value: str
    new_value: str
    rationale: str


@dataclass(frozen=True, slots=True)
class PriceTrigger:
    """Trigger for TAKE_PROFIT and PRICE_STOP legs.

    Evaluates against the underlying equity's real-time price stream
    (per orders-and-brackets.md § Options price-based stops). For equity
    positions, ``underlying_ticker`` is the equity itself; for options
    positions, it is the option's underlying equity.
    """

    underlying_ticker: Symbol
    threshold_usd: float
    direction: Literal["GTE", "LTE"]
    trigger_type: Literal["price"] = field(default="price", init=False)

    def __post_init__(self) -> None:
        if len(self.underlying_ticker) < 1:
            msg = "underlying_ticker must be non-empty"
            raise ValueError(msg)
        if not math.isfinite(self.threshold_usd):
            msg = f"threshold_usd must be finite; got {self.threshold_usd}"
            raise ValueError(msg)
        if self.threshold_usd <= 0:
            msg = f"threshold_usd must be > 0; got {self.threshold_usd}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TimeTrigger:
    """Trigger for TIME_EXPIRATION legs.

    Fires when wall-clock time crosses the deadline.
    """

    deadline: datetime
    trigger_type: Literal["time"] = field(default="time", init=False)

    def __post_init__(self) -> None:
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            msg = "deadline must be tz-aware UTC"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class EventTrigger:
    """Trigger for EVENT_INVALIDATION legs.

    Qualitative condition the analysis pipeline evaluates. The engine
    cannot enforce mechanically; the PM acts on the flag.
    """

    description: str
    condition_evaluator_id: str | None = None
    trigger_type: Literal["event"] = field(default="event", init=False)

    def __post_init__(self) -> None:
        if len(self.description) < 1:
            msg = "description must be non-empty"
            raise ValueError(msg)


TriggerPayload = PriceTrigger | TimeTrigger | EventTrigger


@dataclass(frozen=True, slots=True)
class PLAnchorSpec:
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

    spec_type: Literal["target", "stop"]
    pct: float
    planned_entry_price: float
    actual_entry_price: float | None = None
    recalculated_at_fill: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.pct):
            msg = f"pct must be finite; got {self.pct}"
            raise ValueError(msg)
        if not (0 < self.pct <= 10.0):
            msg = f"pct must satisfy 0 < pct <= 10.0; got {self.pct}"
            raise ValueError(msg)
        if not math.isfinite(self.planned_entry_price):
            msg = f"planned_entry_price must be finite; got {self.planned_entry_price}"
            raise ValueError(msg)
        if self.planned_entry_price <= 0:
            msg = f"planned_entry_price must be > 0; got {self.planned_entry_price}"
            raise ValueError(msg)
        if self.recalculated_at_fill and self.actual_entry_price is None:
            msg = "recalculated_at_fill=True requires actual_entry_price to be non-None"
            raise ValueError(msg)
        if not self.recalculated_at_fill and self.actual_entry_price is not None:
            msg = "actual_entry_price must be None when recalculated_at_fill=False"
            raise ValueError(msg)


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


@dataclass(frozen=True, slots=True)
class BracketLeg:
    """One leg definition within a bracket's protective set."""

    leg_id: str
    leg_type: BracketLegType
    order_id: OrderId | None
    trigger: TriggerPayload
    enforcement: BracketLegEnforcement
    status: BracketLegStatus
    pl_anchor: PLAnchorSpec | None = None
    # ADR-0003: the typed broker-vs-monitor binding. Defaults to
    # MONITOR_ENFORCED — the conservative case (armed Intent with no broker
    # order); the broker-enforced legs (native equity bracket child, options
    # capital floor) are set explicitly by the later bracket stories.
    enforcement_binding: EnforcementBinding = EnforcementBinding.MONITOR_ENFORCED
    # ALP-852 / ADR-0003 — which signal a thesis-invalidation PRICE_STOP fires
    # on, matched to the thesis nature (set by the OPEN writeback from the wire
    # ``PriceLeg.trigger_signal``). The continuous monitor reads it to select the
    # trigger evaluator: ``UNDERLYING_PRICE`` (directional) vs ``OPTION_PRICE`` /
    # ``NET_MARK`` (non-directional). ``None`` on a TAKE_PROFIT / TIME / EVENT leg
    # — those carry no thesis-invalidation trigger signal — and on a legacy
    # PRICE_STOP predating the tag (the monitor reads ``None`` as the legacy
    # underlying-triggered shape).
    trigger_signal: TriggerSignal | None = None

    def __post_init__(self) -> None:
        self._validate_event_invalidation()
        self._validate_trigger_matches_leg_type()
        self._validate_pl_anchor_compatibility()
        self._validate_trigger_signal_leg_type()

    def _validate_event_invalidation(self) -> None:
        if self.leg_type == BracketLegType.EVENT_INVALIDATION and self.order_id is not None:
            msg = "EVENT_INVALIDATION leg must have order_id as None"
            raise ValueError(msg)

    def _validate_trigger_matches_leg_type(self) -> None:
        expected = _LEG_TYPE_TO_TRIGGER_TYPE[self.leg_type]
        if self.trigger.trigger_type != expected:
            msg = (
                f"leg_type={self.leg_type!r} requires trigger_type={expected!r}; "
                f"got trigger_type={self.trigger.trigger_type!r}"
            )
            raise ValueError(msg)

    def _validate_pl_anchor_compatibility(self) -> None:
        if self.pl_anchor is None:
            return
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

    def _validate_trigger_signal_leg_type(self) -> None:
        # ALP-852 — ``trigger_signal`` names the thesis-invalidation stop's
        # signal, so it is only meaningful on a PRICE_STOP leg. A TAKE_PROFIT
        # leg fires on its own target geometry (price target or strategy net
        # P/L), and a TIME / EVENT leg has no price signal at all.
        if self.trigger_signal is not None and self.leg_type is not BracketLegType.PRICE_STOP:
            msg = (
                f"trigger_signal is only valid on a PRICE_STOP thesis-invalidation "
                f"leg; got leg_type={self.leg_type!r}"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BracketRecord:
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

    bracket_id: BracketId
    position_id: PositionId
    status: BracketStatus
    entry_order_id: OrderId
    protective_legs: tuple[BracketLeg, ...]
    modification_history: tuple[BracketLegModification, ...]
    corporate_action_cancellation_reason: str | None
    entry_window_deadline: datetime | None = None

    def __post_init__(self) -> None:
        if self.entry_window_deadline is not None and (
            self.entry_window_deadline.tzinfo is None
            or self.entry_window_deadline.utcoffset() is None
        ):
            msg = "entry_window_deadline must be tz-aware UTC when not None"
            raise ValueError(msg)
        self._check_protective_legs_non_empty()
        self._check_hard_backstop()
        self._check_pending_entry_rule()
        self._check_dissolved_rule()

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
