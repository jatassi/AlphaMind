"""Invariant checks for _build_monitor_close_order_row (FL12 / ALP-842 wave-4).

A monitor-fired close always fires because a protective bracket leg fired.
That means every live close position MUST have a real bracket_id.  Passing
``bracket_id=None`` would silently produce an empty-string FK value that
references no ``brackets`` row → deferred FK violation at commit in
production (``PRAGMA foreign_keys=ON``).  The function must surface the
violation as a clear, actionable error rather than letting it become an
opaque IntegrityError deep inside a transaction.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.continuous_monitor.bracket_stops.close_order_precommit import (
    _build_monitor_close_order_row,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

_NOW = datetime(2026, 5, 27, 20, 0, tzinfo=UTC)
_THESIS_ID = "THE-AAPL-0123456789abcdef0123456789abcdef"


def _position_without_bracket(position_id: str = "pos-no-bracket") -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(_THESIS_ID),
        bracket_id=None,  # the invariant-violating state
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=200.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=2.5,
            greeks=OptionGreeks(delta=0.4, gamma=0.02, theta=-0.01, vega=0.10),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(2.5),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def test_build_monitor_close_order_row_raises_on_none_bracket_id() -> None:
    """bracket_id=None must raise a clear error naming the position, not produce ''.

    The empty-string fallback (``bracket_id or ""``) would silently insert a
    bracket_id='' value that references no brackets row, causing a deferred FK
    violation at commit instead of surfacing the invariant violation here.
    """
    position = _position_without_bracket("pos-no-bracket")

    with pytest.raises(ValueError, match="pos-no-bracket"):
        _build_monitor_close_order_row(position, "order-1", "coid-1")
