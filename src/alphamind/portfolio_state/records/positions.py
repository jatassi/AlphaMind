"""Persistent typed records for position inventory (story 03a + 05a).

Story 05a split the original ``PositionRecord`` into a slim persistent core
(this file) and a delivery-time ``PositionView`` (see ``views/positions.py``).
Computed enrichments (market value, unrealized P/L, exposure, etc.) live on
``PositionView``; ``PositionRecord`` carries only state that survives across
invocations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from alphamind._kernel.ids import BracketId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import Money, Price


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class InstrumentType(StrEnum):
    EQUITY = "EQUITY"
    OPTIONS = "OPTIONS"
    STRATEGY = "STRATEGY"


class OptionContractType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


class PositionStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class LocateStatus(StrEnum):
    LOCATED = "LOCATED"
    AT_RISK_OF_RECALL = "AT_RISK_OF_RECALL"


@dataclass(frozen=True, slots=True)
class OptionGreeks:
    """Greeks for an options position.

    Sign conventions (per Black-Scholes textbook):

    * delta: positive for long calls (0 to 1), negative for long puts (-1 to 0).
      For short positions, the parent OptionsPositionDetails sign-flips externally
      via the position-level direction; this record stores the *long-equivalent*
      delta of the contract itself.
    * gamma: always positive (curvature of delta wrt underlying).
    * theta: NEGATIVE for long options (decay reduces option value over time);
      consumers needing the position-level theta must sign-flip for short positions.
    * vega: always positive (sensitivity to IV; higher IV always raises long
      option prices).

    See ``docs/design/05-execution-layer/architecture.md`` § 4d for the refresh
    cadence (15-min scheduled + 2%-move-based) and the IV-fetch failure policy
    that governs the freshness metadata fields below.
    """

    delta: float
    gamma: float
    theta: float
    vega: float

    # Freshness metadata (added per architecture.md § 4d)
    as_of_timestamp: datetime | None = None
    iv_used: float | None = None
    refresh_failed: bool = False

    def __post_init__(self) -> None:
        if self.as_of_timestamp is not None and (
            self.as_of_timestamp.tzinfo is None or self.as_of_timestamp.utcoffset() is None
        ):
            msg = "as_of_timestamp must be tz-aware UTC when not None"
            raise ValueError(msg)
        if self.iv_used is not None and self.iv_used <= 0:
            msg = f"iv_used must be > 0 when not None; got {self.iv_used}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class LiveExecutionEstimate:
    """Paper-mode harness estimate of live-execution drag for a single fill.

    Attached by the paper-evaluation harness to fills produced in paper mode.
    Absent in live mode (the broker reports actual costs separately). All four
    fields use the same sign convention as the parent PositionFill:

    * estimated_spread_usd: positive (cost). The estimated bid-ask spread the
      live order would have crossed.
    * estimated_impact_usd: positive (cost). The estimated market-impact drag
      from order size.
    * estimated_regulatory_fees_usd: positive (cost). SEC/FINRA/exchange fees
      Alpaca reports at EOD via the activity feed, not per-fill.
    * live_adjusted_fill_price: the raw paper fill price minus (for buys) or
      plus (for sells) the per-share equivalent of the three drag components.
      Can be above or below the raw fill_price depending on direction.

    ALP-489 — USD/price fields carry ``Money`` / ``Price`` (Decimal-backed).
    The boundary constructors (``money``/``price``) enforce non-negative /
    strictly-positive at the typed-record edge, so a per-field re-check here
    would be redundant.
    """

    estimated_spread_usd: Money
    estimated_impact_usd: Money
    estimated_regulatory_fees_usd: Money
    live_adjusted_fill_price: Price


@dataclass(frozen=True, slots=True)
class PositionFill:
    """Bare-minimum execution audit per position.

    Sign conventions:

    * slippage: signed. Positive when the fill price was worse than the
      reference price at submission (buy filled higher / sell filled lower).
      Negative when the fill price was better (price improvement). Reference
      price is the limit price for limit orders, and the mid-quote at
      submission for market orders.
    * fees: always positive (cost — broker, regulatory, exchange).

    ALP-489 — USD/price fields carry ``Money`` / ``Price``. ``signed_money``
    accommodates negative slippage; ``money``/``price`` enforce
    non-negative / strictly-positive at the boundary.
    """

    fill_timestamp: datetime
    fill_price: Price
    fill_quantity: float
    slippage: Money
    fees: Money
    live_execution_estimate: LiveExecutionEstimate | None = None


@dataclass(frozen=True, slots=True)
class EquityPositionDetails:
    """Equity position details; short-only fields are None for long positions.

    Field semantics for the short-only fields:

    * ``borrow_rate_pct`` — the broker's borrow rate (annualized %) stamped at
      entry. Not updated on ADD fills; the borrow-accrual monitor reads the
      live rate from broker each tick to compute accrual. The stamped value is
      a snapshot of the rate at the first entry fill.
    * ``accrued_borrow_cost_usd`` — running borrow accumulator. Updated only by
      the borrow-accrual monitor (tick-based recomputation against the live
      broker rate); the write-path preserves it verbatim on ADD fills.
    * ``locate_status`` — borrow locate state (LOCATED / AT_RISK_OF_RECALL).
      Preserved verbatim on ADD fills; updated only by the breach/locate
      monitor when broker indicates recall risk.
    * ``margin_held_usd`` — **entry-stamp Reg T initial margin**, captured at
      first entry as ``qty × entry_price × 0.50``. NOT updated on ADD fills —
      readers needing live required margin should compute
      ``qty × current_price × 0.50`` from ``share_count`` and the live close.
      This field is a frozen entry snapshot, useful for audit/attribution; it
      is not the broker's current required margin.
    """

    ticker: Symbol
    share_count: float
    average_cost_basis_per_share: float
    borrow_rate_pct: float | None = None
    accrued_borrow_cost_usd: float | None = None
    locate_status: LocateStatus | None = None
    margin_held_usd: float | None = None
    instrument_type: InstrumentType = field(default=InstrumentType.EQUITY, init=False)


@dataclass(frozen=True, slots=True)
class OptionsPositionDetails:
    """Options contract details."""

    underlying_ticker: Symbol
    strike_price: float
    expiration_date: date
    contract_type: OptionContractType
    contract_count: float
    contract_multiplier: float
    premium_paid_per_contract: float
    greeks: OptionGreeks
    instrument_type: InstrumentType = field(default=InstrumentType.OPTIONS, init=False)


@dataclass(frozen=True, slots=True)
class StrategyLeg:
    """Single leg of a multi-leg options strategy."""

    leg_id: str
    options: OptionsPositionDetails
    direction: Direction | None = None


@dataclass(frozen=True, slots=True)
class StrategyPositionDetails:
    """Multi-leg options strategy details."""

    strategy_type_label: str
    legs: tuple[StrategyLeg, ...]
    net_premium_usd: float
    max_profit_usd: float
    max_loss_usd: float
    breakeven_levels: tuple[float, ...]
    strategy_greeks: OptionGreeks
    instrument_type: InstrumentType = field(default=InstrumentType.STRATEGY, init=False)


PositionDetailsPayload = EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails


def resolve_ticker(
    details: EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails,
) -> Symbol | None:
    """Extract the underlying ticker from a position-details payload.

    Returns the ticker for equity and options positions, the first leg's
    underlying ticker for multi-leg strategies, or ``None`` when a strategy has
    no legs. Callers that need an empty-string sentinel on miss should adapt
    locally via ``resolve_ticker(details) or ""``.
    """
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    if isinstance(details, StrategyPositionDetails) and details.legs:
        return details.legs[0].options.underlying_ticker
    return None


def occ_symbol_for_options(details: OptionsPositionDetails) -> str:
    """Build the OCC contract symbol the collector writes to ``options_contract_snapshots``.

    Format: ``O:{UNDERLYING}{YYMMDD}{C|P}{strike_milli}`` where ``strike_milli``
    is the strike multiplied by 1000, zero-padded to 8 digits. This is the
    Polygon convention used by ``data_sources/polygon/options.py`` and the
    industry-standard OCC encoding. Both the greeks-refresh task and the
    snapshot assembler's option-price reader key into the same table by this
    symbol, so the single canonical builder lives here next to
    :class:`OptionsPositionDetails`.
    """
    expiry = details.expiration_date.strftime("%y%m%d")
    cp = "C" if details.contract_type is OptionContractType.CALL else "P"
    strike_milli = round(details.strike_price * 1000)
    return f"O:{details.underlying_ticker}{expiry}{cp}{strike_milli:08d}"


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """Persistent record for a single position across all instrument types.

    This record carries only state that survives across invocations. Computed
    enrichments (market value, unrealized P/L, exposure, etc.) live on
    ``PositionView`` and are produced by the snapshot assembler at delivery
    time.
    """

    position_id: PositionId
    thesis_id: ThesisId | None
    bracket_id: BracketId | None
    status: PositionStatus
    direction: Direction | None
    entry_timestamp: datetime | None
    details: PositionDetailsPayload

    execution_history: tuple[PositionFill, ...]
    realized_pnl_to_date_usd: float | None

    corporate_action_adjustment_needed: bool
    parent_position_id: PositionId | None
    origin: str | None

    @property
    def instrument_type(self) -> InstrumentType:
        """Derived from the discriminated ``details`` payload."""
        return self.details.instrument_type

    def __post_init__(self) -> None:
        self._check_direction_optionality()
        self._check_status_rules()
        self._check_equity_direction_fields()
        self._check_spinoff_invariant()

    def _check_direction_optionality(self) -> None:
        """``direction is None`` iff ``details`` is a strategy payload.

        Position-level direction is a category error for a multi-leg strategy
        (an iron condor is neither long nor short), so a strategy record
        carries ``direction = None`` and reads its directional sign per-leg.
        An equity or single-leg options record is long or short, so it
        requires a non-``None`` ``Direction``. :func:`position_direction` is
        the canonical accessor.
        """
        is_strategy = isinstance(self.details, StrategyPositionDetails)
        if is_strategy and self.direction is not None:
            msg = "direction must be None for a strategy position"
            raise ValueError(msg)
        if not is_strategy and self.direction is None:
            msg = "direction must be non-None for an equity or options position"
            raise ValueError(msg)

    def _check_status_rules(self) -> None:
        # Strategy positions accumulate per-leg fills in execution_history while
        # the parent stays PENDING — the atomic PENDING → OPEN transition fires
        # only when every leg has reached `filled` status (broker-adapter.md §
        # Multi-leg fill events § Atomicity). The "PENDING → empty history"
        # invariant continues to apply for equity / single-leg options.
        if (
            self.status == PositionStatus.PENDING
            and self.execution_history
            and not isinstance(self.details, StrategyPositionDetails)
        ):
            msg = "execution_history must be empty when status is PENDING"
            raise ValueError(msg)
        if self.status == PositionStatus.OPEN and not self.execution_history:
            msg = "execution_history must be non-empty when status is OPEN"
            raise ValueError(msg)
        if self.status == PositionStatus.CLOSED and self.realized_pnl_to_date_usd is None:
            msg = "realized_pnl_to_date_usd must be non-None when status is CLOSED"
            raise ValueError(msg)

    def _check_equity_direction_fields(self) -> None:
        if not isinstance(self.details, EquityPositionDetails):
            return
        short_fields = (
            self.details.borrow_rate_pct,
            self.details.accrued_borrow_cost_usd,
            self.details.locate_status,
            self.details.margin_held_usd,
        )
        if self.direction == Direction.SHORT and any(f is None for f in short_fields):
            msg = (
                "borrow_rate_pct, accrued_borrow_cost_usd, locate_status, and margin_held_usd "
                "must all be non-None when direction is SHORT"
            )
            raise ValueError(msg)
        if self.direction == Direction.LONG and any(f is not None for f in short_fields):
            msg = (
                "borrow_rate_pct, accrued_borrow_cost_usd, locate_status, and margin_held_usd "
                "must all be None when direction is LONG"
            )
            raise ValueError(msg)

    def _check_spinoff_invariant(self) -> None:
        if self.origin is not None:
            if self.parent_position_id is None:
                msg = "parent_position_id must be non-None when origin is set"
                raise ValueError(msg)
            if not self.corporate_action_adjustment_needed:
                msg = "corporate_action_adjustment_needed must be True when origin is set"
                raise ValueError(msg)


def position_direction(record: PositionRecord) -> Direction | None:
    """Return the position-level directional sign, instrument-aware.

    An equity or single-leg options position is long or short, so the accessor
    returns ``record.direction``. A multi-leg strategy is neither — its
    directionality lives per-leg on each :class:`StrategyLeg` — and the record
    validator ties ``direction is None`` to a strategy payload, so the accessor
    returns ``None`` for a strategy.

    This is the one accessor for position-level direction: consumers must not
    read ``PositionRecord.direction`` directly. A consumer holding a
    ``PositionView`` calls ``position_direction(view.record)``.
    """
    return record.direction
