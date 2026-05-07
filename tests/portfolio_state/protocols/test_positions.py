"""Tests for BasePositionProtocol (story 05b).

The Protocol formalises the design's "base position interface" — the field set
shared by every instrument type. Consumers reasoning generically about
positions can annotate against the Protocol; per
``docs/design/05-execution-layer/position-model.md`` § Base position.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind.portfolio_state.protocols.positions import BasePositionProtocol
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Builders — mirror the canonical fixtures used by views/test_positions.py
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_FILL = PositionFill(
    fill_timestamp=_NOW,
    fill_price=150.0,
    fill_quantity=100.0,
    slippage=0.01,
    fees=1.0,
)
_LONG_EQUITY = EquityPositionDetails(
    ticker="AAPL",
    share_count=100.0,
    average_cost_basis_per_share=150.0,
)


def _make_record(**overrides: object) -> PositionRecord:
    kwargs: dict[str, object] = {
        "position_id": "POS-AAPL-001",
        "thesis_id": "THESIS-001",
        "bracket_id": None,
        "status": PositionStatus.OPEN,
        "direction": Direction.LONG,
        "entry_timestamp": _NOW,
        "details": _LONG_EQUITY,
        "execution_history": (_FILL,),
        "realized_pnl_to_date_usd": None,
        "corporate_action_adjustment_needed": False,
        "parent_position_id": None,
        "origin": None,
    }
    kwargs.update(overrides)
    return PositionRecord.model_validate(kwargs)


def _make_view(**overrides: object) -> PositionView:
    record = _make_record()
    kwargs: dict[str, object] = {
        "record": record,
        "current_market_value_usd": 15_000.0,
        "unrealized_pnl_usd": 500.0,
        "unrealized_pnl_pct": 3.4,
        "position_weight_pct": 10.0,
        "position_age_hours": 24.0,
        "notional_exposure_usd": 15_000.0,
        "delta_adjusted_exposure_usd": 15_000.0,
        "distance_to_target_usd": None,
        "distance_to_stop_usd": None,
        "risk_reward_at_current": None,
    }
    kwargs.update(overrides)
    return PositionView.model_validate(kwargs)


# ---------------------------------------------------------------------------
# Acceptance criteria — runtime structural conformance
# ---------------------------------------------------------------------------


def test_position_record_satisfies_base_protocol() -> None:
    """PositionRecord exposes every field the design's base interface names."""
    record = _make_record()
    assert isinstance(record, BasePositionProtocol)


def test_position_view_satisfies_base_protocol_via_convenience_properties() -> None:
    """PositionView reaches the base interface via 05a's pass-through properties."""
    view = _make_view()
    assert isinstance(view, BasePositionProtocol)


def test_dict_does_not_satisfy_base_protocol() -> None:
    """A bare dict (no property accessors) fails the structural check."""
    not_a_position: dict[str, object] = {
        "position_id": "POS-001",
        "thesis_id": None,
        "bracket_id": None,
        "status": PositionStatus.OPEN,
        "direction": Direction.LONG,
        "entry_timestamp": _NOW,
    }
    assert not isinstance(not_a_position, BasePositionProtocol)


def test_order_record_does_not_satisfy_base_protocol() -> None:
    """OrderRecord lacks the design's base position fields (e.g. entry_timestamp)."""
    from alphamind.portfolio_state.records.orders import (
        EquityInstrumentSpec,
        OrderClass,
        OrderDirection,
        OrderDuration,
        OrderRecord,
        OrderRole,
        OrderStatus,
        OrderType,
        PriceParameters,
    )

    order = OrderRecord(
        order_id="ORD-001",
        position_id=None,
        bracket_id="BR-001",
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker="AAPL"),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=100.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id="ALP-1",
        alpaca_order_id_chain=("ALP-1",),
        submission_timestamp=_NOW,
        last_update_timestamp=_NOW,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=100.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=0.0,
    )
    assert not isinstance(order, BasePositionProtocol)


# ---------------------------------------------------------------------------
# Field set coverage
# ---------------------------------------------------------------------------


def test_protocol_attrs_match_design_base_interface() -> None:
    """Acceptance: protocol fields cover position_id, thesis_id, bracket_id,
    status, direction, entry_timestamp — the design's base-interface set."""
    expected_fields = {
        "position_id",
        "thesis_id",
        "bracket_id",
        "status",
        "direction",
        "entry_timestamp",
    }
    # ``__protocol_attrs__`` is a CPython runtime implementation detail used
    # by ``runtime_checkable``; the typing stubs don't expose it, so cast to
    # ``Any`` for the lookup.
    proto: Any = BasePositionProtocol
    actual_fields = set(proto.__protocol_attrs__)
    assert actual_fields == expected_fields


# ---------------------------------------------------------------------------
# Field-value access through the Protocol annotation
# ---------------------------------------------------------------------------


def test_protocol_accessors_return_record_values() -> None:
    """Reading through the Protocol yields the same values as concrete record."""
    record = _make_record()
    base: BasePositionProtocol = record
    assert base.position_id == "POS-AAPL-001"
    assert base.thesis_id == "THESIS-001"
    assert base.bracket_id is None
    assert base.status is PositionStatus.OPEN
    assert base.direction is Direction.LONG
    assert base.entry_timestamp == _NOW


def test_protocol_accessors_return_view_values() -> None:
    """Reading through the Protocol on a view delegates to the wrapped record."""
    view = _make_view()
    base: BasePositionProtocol = view
    assert base.position_id == "POS-AAPL-001"
    assert base.thesis_id == "THESIS-001"
    assert base.status is PositionStatus.OPEN
    assert base.direction is Direction.LONG
