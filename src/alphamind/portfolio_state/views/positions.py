"""Delivery-time projection of a PositionRecord (story 05a).

``PositionView`` wraps a persistent ``PositionRecord`` and adds the ten computed
fields the snapshot assembler derives from market data and total portfolio
value at delivery time. The split eliminates the drift class where producers
could construct records with arbitrary computed values that disagreed with the
canonical functions in ``computations/positions.py``.

Construction discipline: only the snapshot assembler produces ``PositionView``
instances. Consumers receive views and must not construct them directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionDetailsPayload,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
_NonNegFiniteFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


class PositionView(BaseModel):
    """Delivery-time projection of a PositionRecord with computed enrichments.

    Constructed by the snapshot assembler at each invocation. Computed fields
    are derived from PositionRecord + current market data + total portfolio
    value via the canonical functions in ``computations/positions.py``.
    Producers outside the assembler must not construct PositionView directly.

    The ``record`` field gives consumers transparent access to the persistent
    position state (``view.record.position_id``, ``view.record.thesis_id``,
    ``view.record.direction``, ``view.record.details``, ...). Convenience
    pass-through properties below mirror the most frequently-used persistent
    fields so existing consumer call sites remain ``view.position_id``,
    ``view.direction``, etc. — minimising churn during the 05a migration.

    Sign conventions
    ----------------
    ``position_weight_pct``
        Signed: positive for long positions, negative for short positions. Can
        exceed 100% absolute value when the position is leveraged. Any finite
        float is accepted.

    ``notional_exposure_usd``
        Magnitude only — always >= 0. Represents the gross notional of the
        position regardless of direction.

    ``delta_adjusted_exposure_usd``
        Signed: positive for net-long delta, negative for net-short delta.
        For short equities this is negative; for options it is signed by the
        option delta. Any finite float is accepted.
    """

    model_config = ConfigDict(frozen=True)

    record: PositionRecord
    current_market_value_usd: _FiniteFloat
    unrealized_pnl_usd: _FiniteFloat
    unrealized_pnl_pct: _FiniteFloat
    position_weight_pct: _FiniteFloat
    position_age_hours: _NonNegFiniteFloat
    notional_exposure_usd: _NonNegFiniteFloat
    delta_adjusted_exposure_usd: _FiniteFloat
    distance_to_target_usd: float | None
    distance_to_stop_usd: float | None
    risk_reward_at_current: float | None

    # ------------------------------------------------------------------
    # Convenience pass-through properties — mirror persistent record fields
    # ------------------------------------------------------------------

    @property
    def position_id(self) -> str:
        return self.record.position_id

    @property
    def thesis_id(self) -> str | None:
        return self.record.thesis_id

    @property
    def bracket_id(self) -> str | None:
        return self.record.bracket_id

    @property
    def status(self) -> PositionStatus:
        return self.record.status

    @property
    def direction(self) -> Direction:
        return self.record.direction

    @property
    def entry_timestamp(self) -> datetime | None:
        return self.record.entry_timestamp

    @property
    def details(self) -> PositionDetailsPayload:
        return self.record.details

    @property
    def instrument_type(self) -> InstrumentType:
        return self.record.instrument_type

    @property
    def execution_history(self) -> tuple[PositionFill, ...]:
        return self.record.execution_history

    @property
    def realized_pnl_to_date_usd(self) -> float | None:
        return self.record.realized_pnl_to_date_usd

    @property
    def corporate_action_adjustment_needed(self) -> bool:
        return self.record.corporate_action_adjustment_needed

    @property
    def parent_position_id(self) -> str | None:
        return self.record.parent_position_id

    @property
    def origin(self) -> str | None:
        return self.record.origin


__all__ = ["PositionView"]
