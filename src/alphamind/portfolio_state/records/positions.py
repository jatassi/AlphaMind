"""Consumer-facing typed records for position inventory (story 03a)."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


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


class PositionFill(BaseModel):
    """Bare-minimum execution audit per position."""

    model_config = ConfigDict(frozen=True)

    fill_timestamp: datetime
    fill_price: float
    fill_quantity: float
    slippage: float
    fees: float


class EquityPositionDetails(BaseModel):
    """Equity position details; short-only fields are None for long positions."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    share_count: float
    average_cost_basis_per_share: float
    borrow_rate_pct: float | None = None
    locate_status: LocateStatus | None = None
    margin_held_usd: float | None = None


class OptionsPositionDetails(BaseModel):
    """Options contract details."""

    model_config = ConfigDict(frozen=True)

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

    strategy_type_label: str
    legs: tuple[StrategyLeg, ...]
    net_premium_usd: float
    max_profit_usd: float
    max_loss_usd: float
    breakeven_levels: tuple[float, ...]
    strategy_greeks: OptionGreeks


class PositionRecord(BaseModel):
    """Consumer-facing record for a single position across all instrument types.

    Sign conventions
    ----------------
    ``position_weight_pct``
        Signed: positive for long positions, negative for short positions. Can
        exceed 100% absolute value when the position is leveraged. Computed as
        ``current_market_value_usd / total_portfolio_value * 100``; the sign
        follows the market value sign. Any finite float is accepted.

    ``notional_exposure_usd``
        Magnitude only — always >= 0. Represents the gross notional of the
        position regardless of direction.

    ``delta_adjusted_exposure_usd``
        Signed: positive for net-long delta, negative for net-short delta.
        For short equities this is negative; for options it is signed by the
        option delta. Any finite float is accepted.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    thesis_id: str | None
    bracket_id: str | None
    status: PositionStatus
    direction: Direction
    entry_timestamp: datetime | None
    instrument_type: InstrumentType

    # Exactly one non-None, matching instrument_type — enforced by _check_discriminator
    equity_details: EquityPositionDetails | None = None
    options_details: OptionsPositionDetails | None = None
    strategy_details: StrategyPositionDetails | None = None

    execution_history: tuple[PositionFill, ...]
    realized_pnl_to_date_usd: float | None

    # Shape declared here; population is the assembler's job (story 06)
    current_market_value_usd: float
    unrealized_pnl_usd: float
    unrealized_pnl_pct: float
    position_weight_pct: _FiniteFloat
    position_age_hours: float
    notional_exposure_usd: float
    delta_adjusted_exposure_usd: _FiniteFloat
    distance_to_target_usd: float | None
    distance_to_stop_usd: float | None
    risk_reward_at_current: float | None

    corporate_action_adjustment_needed: bool
    parent_position_id: str | None
    origin: str | None

    @model_validator(mode="after")
    def _validate_all(self) -> PositionRecord:
        self._check_discriminator()
        self._check_status_rules()
        self._check_equity_direction_fields()
        self._check_range_constraints()
        self._check_spinoff_invariant()
        return self

    def _check_discriminator(self) -> None:
        populated = [
            name
            for name, val in [
                ("equity_details", self.equity_details),
                ("options_details", self.options_details),
                ("strategy_details", self.strategy_details),
            ]
            if val is not None
        ]
        if len(populated) != 1:
            msg = (
                f"Exactly one of equity_details, options_details, strategy_details must be "
                f"non-None; got {len(populated)} non-None: {populated}"
            )
            raise ValueError(msg)
        field_name = populated[0]
        expected_map = {
            InstrumentType.EQUITY: "equity_details",
            InstrumentType.OPTIONS: "options_details",
            InstrumentType.STRATEGY: "strategy_details",
        }
        expected = expected_map[self.instrument_type]
        if field_name != expected:
            msg = (
                f"instrument_type={self.instrument_type!r} requires {expected!r} "
                f"to be non-None, but {field_name!r} is set instead"
            )
            raise ValueError(msg)

    def _check_status_rules(self) -> None:
        if self.status == PositionStatus.PENDING:
            if self.execution_history:
                msg = "execution_history must be empty when status is PENDING"
                raise ValueError(msg)
        elif self.status == PositionStatus.OPEN:
            if not self.execution_history:
                msg = "execution_history must be non-empty when status is OPEN"
                raise ValueError(msg)
        elif self.status == PositionStatus.CLOSED:
            if self.realized_pnl_to_date_usd is None:
                msg = "realized_pnl_to_date_usd must be non-None when status is CLOSED"
                raise ValueError(msg)
            if self.current_market_value_usd != 0.0:
                msg = "current_market_value_usd must be zero when status is CLOSED"
                raise ValueError(msg)
            if self.unrealized_pnl_usd != 0.0:
                msg = "unrealized_pnl_usd must be zero when status is CLOSED"
                raise ValueError(msg)

    def _check_equity_direction_fields(self) -> None:
        if self.equity_details is None:
            return
        short_fields = (
            self.equity_details.borrow_rate_pct,
            self.equity_details.locate_status,
            self.equity_details.margin_held_usd,
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

    def _check_range_constraints(self) -> None:
        if self.position_age_hours < 0.0:
            msg = f"position_age_hours must be >= 0; got {self.position_age_hours}"
            raise ValueError(msg)
        if self.notional_exposure_usd < 0.0:
            msg = f"notional_exposure_usd must be >= 0; got {self.notional_exposure_usd}"
            raise ValueError(msg)

    def _check_spinoff_invariant(self) -> None:
        if self.origin is not None:
            if self.parent_position_id is None:
                msg = "parent_position_id must be non-None when origin is set"
                raise ValueError(msg)
            if not self.corporate_action_adjustment_needed:
                msg = "corporate_action_adjustment_needed must be True when origin is set"
                raise ValueError(msg)
