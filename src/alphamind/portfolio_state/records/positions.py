"""Persistent typed records for position inventory (story 03a + 05a).

Story 05a split the original ``PositionRecord`` into a slim persistent core
(this file) and a delivery-time ``PositionView`` (see ``views/positions.py``).
Computed enrichments (market value, unrealized P/L, exposure, etc.) live on
``PositionView``; ``PositionRecord`` carries only state that survives across
invocations.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
_NonNegFiniteFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


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


class OptionGreeks(BaseModel):
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

    model_config = ConfigDict(frozen=True)

    delta: float
    gamma: float
    theta: float
    vega: float

    # Freshness metadata (added per architecture.md § 4d)
    as_of_timestamp: datetime | None = None
    iv_used: float | None = None
    refresh_failed: bool = False

    @field_validator("as_of_timestamp")
    @classmethod
    def _require_tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            msg = "as_of_timestamp must be tz-aware UTC when not None"
            raise ValueError(msg)
        return v

    @field_validator("iv_used")
    @classmethod
    def _require_positive_iv(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            msg = f"iv_used must be > 0 when not None; got {v}"
            raise ValueError(msg)
        return v


class LiveExecutionEstimate(BaseModel):
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
      Always finite; can be above or below the raw fill_price depending on
      direction.
    """

    model_config = ConfigDict(frozen=True)

    estimated_spread_usd: _NonNegFiniteFloat
    estimated_impact_usd: _NonNegFiniteFloat
    estimated_regulatory_fees_usd: _NonNegFiniteFloat
    live_adjusted_fill_price: _FiniteFloat


class PositionFill(BaseModel):
    """Bare-minimum execution audit per position.

    Sign conventions:

    * slippage: signed. Positive when the fill price was worse than the
      reference price at submission (buy filled higher / sell filled lower).
      Negative when the fill price was better (price improvement). Reference
      price is the limit price for limit orders, and the mid-quote at
      submission for market orders.
    * fees: always positive (cost — broker, regulatory, exchange).
    """

    model_config = ConfigDict(frozen=True)

    fill_timestamp: datetime
    fill_price: float
    fill_quantity: float
    slippage: float
    fees: Annotated[float, Field(ge=0.0)]
    live_execution_estimate: LiveExecutionEstimate | None = None


class EquityPositionDetails(BaseModel):
    """Equity position details; short-only fields are None for long positions."""

    model_config = ConfigDict(frozen=True)

    instrument_type: Literal[InstrumentType.EQUITY] = InstrumentType.EQUITY
    ticker: str
    share_count: float
    average_cost_basis_per_share: float
    borrow_rate_pct: float | None = None
    locate_status: LocateStatus | None = None
    margin_held_usd: float | None = None


class OptionsPositionDetails(BaseModel):
    """Options contract details."""

    model_config = ConfigDict(frozen=True)

    instrument_type: Literal[InstrumentType.OPTIONS] = InstrumentType.OPTIONS
    underlying_ticker: str
    strike_price: float
    expiration_date: date
    contract_type: OptionContractType
    contract_count: float
    contract_multiplier: float
    premium_paid_per_contract: float
    greeks: OptionGreeks


class StrategyLeg(BaseModel):
    """Single leg of a multi-leg options strategy."""

    model_config = ConfigDict(frozen=True)

    leg_id: str
    direction: Direction | None = None
    options: OptionsPositionDetails


class StrategyPositionDetails(BaseModel):
    """Multi-leg options strategy details."""

    model_config = ConfigDict(frozen=True)

    instrument_type: Literal[InstrumentType.STRATEGY] = InstrumentType.STRATEGY
    strategy_type_label: str
    legs: tuple[StrategyLeg, ...]
    net_premium_usd: float
    max_profit_usd: float
    max_loss_usd: float
    breakeven_levels: tuple[float, ...]
    strategy_greeks: OptionGreeks


PositionDetailsPayload = Annotated[
    EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails,
    Field(discriminator="instrument_type"),
]


class PositionRecord(BaseModel):
    """Persistent record for a single position across all instrument types.

    This record carries only state that survives across invocations. Computed
    enrichments (market value, unrealized P/L, exposure, etc.) live on
    ``PositionView`` and are produced by the snapshot assembler at delivery
    time.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    thesis_id: str | None
    bracket_id: str | None
    status: PositionStatus
    direction: Direction
    entry_timestamp: datetime | None
    details: PositionDetailsPayload

    execution_history: tuple[PositionFill, ...]
    realized_pnl_to_date_usd: float | None

    corporate_action_adjustment_needed: bool
    parent_position_id: str | None
    origin: str | None

    @property
    def instrument_type(self) -> InstrumentType:
        """Derived from the discriminated ``details`` payload."""
        return self.details.instrument_type

    @model_validator(mode="after")
    def _validate_all(self) -> PositionRecord:
        self._check_status_rules()
        self._check_equity_direction_fields()
        self._check_spinoff_invariant()
        return self

    def _check_status_rules(self) -> None:
        if self.status == PositionStatus.PENDING and self.execution_history:
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
            self.details.locate_status,
            self.details.margin_held_usd,
        )
        if self.direction == Direction.SHORT and any(f is None for f in short_fields):
            msg = (
                "borrow_rate_pct, locate_status, and margin_held_usd must all be non-None "
                "when direction is SHORT"
            )
            raise ValueError(msg)
        if self.direction == Direction.LONG and any(f is not None for f in short_fields):
            msg = (
                "borrow_rate_pct, locate_status, and margin_held_usd must all be None "
                "when direction is LONG"
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
