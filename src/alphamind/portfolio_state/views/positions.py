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

import math
from dataclasses import dataclass
from datetime import datetime

from alphamind._kernel.money import Money
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionDetailsPayload,
    PositionFill,
    PositionRecord,
    PositionStatus,
)


def _check_finite(value: float, field_name: str) -> None:
    if not math.isfinite(value):
        msg = f"{field_name} must be finite; got {value}"
        raise ValueError(msg)


def _check_non_negative_finite(value: float, field_name: str) -> None:
    _check_finite(value, field_name)
    if value < 0:
        msg = f"{field_name} must be >= 0; got {value}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PositionView:
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
        option delta.

    ALP-489 — USD fields carry ``Money`` (Decimal-backed) so the snapshot-time
    rollups (``PortfolioPnL``, ``SectorExposureEntry``, ``DirectionalExposure``)
    aggregate via Decimal arithmetic with no float-sum precision loss.
    ``signed_money`` accommodates negative values (short market value, P/L
    losses, distance-to-target below stop, etc.). Percentage / hours / ratio
    fields remain ``float`` (derived ratios, not money preservation
    quantities).
    """

    record: PositionRecord
    current_market_value_usd: Money
    unrealized_pnl_usd: Money
    unrealized_pnl_pct: float
    position_weight_pct: float
    position_age_hours: float
    notional_exposure_usd: Money
    delta_adjusted_exposure_usd: Money
    distance_to_target_usd: Money | None
    distance_to_stop_usd: Money | None
    risk_reward_at_current: float | None

    def __post_init__(self) -> None:
        _check_finite(self.unrealized_pnl_pct, "unrealized_pnl_pct")
        _check_finite(self.position_weight_pct, "position_weight_pct")
        _check_non_negative_finite(self.position_age_hours, "position_age_hours")

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
