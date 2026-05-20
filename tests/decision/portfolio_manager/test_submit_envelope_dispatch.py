"""Direction-read coverage for the ``submit_envelope`` dispatch resolvers — ALP-609.

The per-command dispatcher-context builders (``_close_command_context`` /
``_add_command_context``) project a persisted position's ``direction`` into the
broker-routing kwargs. After ALP-609 the position-direction read routes through
``position_direction()`` with an explicit non-``None`` assertion — equity and
single-leg options always carry a ``Direction``; the assertion documents the
instrument-narrowing invariant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.commands.command_models import AddCommand, CloseCommand
from alphamind.decision.portfolio_manager.submit_envelope.dispatch import (
    _add_command_context,
    _close_command_context,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import record_to_row

_FILL_TS = datetime(2026, 5, 1, 14, 30, tzinfo=UTC)


class _StubSession:
    """Minimal async session stub — ``get`` returns the seeded ``PositionRow``."""

    def __init__(self, row: PositionRow) -> None:
        self._row = row

    async def get(self, _model: type[PositionRow], position_id: str) -> PositionRow | None:
        return self._row if self._row.position_id == position_id else None


class _StubHandle:
    """Minimal ``invocation_handle`` carrying only the session the resolvers use."""

    def __init__(self, row: PositionRow) -> None:
        self.session = _StubSession(row)


def _fill() -> PositionFill:
    return PositionFill(
        fill_timestamp=_FILL_TS,
        fill_price=price(100.0),
        fill_quantity=10.0,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )


def _equity_position() -> PositionRecord:
    equity = EquityPositionDetails(
        ticker=Symbol("NVDA"),
        share_count=10.0,
        average_cost_basis_per_share=100.0,
    )
    return PositionRecord(
        position_id=PositionId("POS-NVDA-001"),
        thesis_id=ThesisId("TH-NVDA-001"),
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_FILL_TS,
        details=equity,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _options_position() -> PositionRecord:
    options = OptionsPositionDetails(
        underlying_ticker=Symbol("NVDA"),
        strike_price=120.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=2.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=3.50,
        greeks=OptionGreeks(delta=0.45, gamma=0.04, theta=-0.03, vega=0.20),
    )
    return PositionRecord(
        position_id=PositionId("POS-NVDA-OPT-001"),
        thesis_id=ThesisId("TH-NVDA-OPT-001"),
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_FILL_TS,
        details=options,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _close_command(position_id: str) -> CloseCommand:
    return CloseCommand.model_validate(
        {
            "command_type": "close",
            "position_id": position_id,
            "quantity": "all",
            "order_type": "market",
            "limit_price": None,
            "close_rationale_type": "thesis_invalidated",
            "invalidation_reason": "Thesis broken.",
            "risk_management_subtype": None,
        }
    )


def _add_command(position_id: str) -> AddCommand:
    from alphamind.commands.command_models import EntryOrder
    from alphamind.commands.command_models import ThesisComponent as OMSThesisComponent

    return AddCommand(
        command_type="add",
        position_id=PositionId(position_id),
        additional_quantity=5.0,
        additional_dollar_value=money(5_000.0),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        thesis_addition_component=OMSThesisComponent(
            component_type="entry_rationale",
            linked_leg="add",
            instrument_reference="NVDA",
            narrative="Add to NVDA.",
            key_assumptions=("Setup intact.",),
        ),
        bracket_adjustment=None,
    )


def _handle_for(record: PositionRecord) -> _StubHandle:
    return _StubHandle(record_to_row(record))


@pytest.mark.asyncio
async def test_close_equity_long_threads_long_side() -> None:
    """A LONG equity CLOSE threads ``position_side='long'``."""
    record = _equity_position()
    ctx = await _close_command_context(
        _close_command("POS-NVDA-001"), invocation_handle=_handle_for(record)
    )
    assert ctx["position_asset_type"] == "equity"
    assert ctx["position_side"] == "long"


@pytest.mark.asyncio
async def test_close_options_long_threads_sell_to_close() -> None:
    """A LONG options CLOSE threads ``position_intent='sell_to_close'``."""
    record = _options_position()
    ctx = await _close_command_context(
        _close_command("POS-NVDA-OPT-001"), invocation_handle=_handle_for(record)
    )
    assert ctx["position_asset_type"] == "option"
    assert ctx["position_intent"] == "sell_to_close"


@pytest.mark.asyncio
async def test_add_equity_long_threads_long_side() -> None:
    """A LONG equity ADD threads ``position_side='long'``."""
    record = _equity_position()
    ctx = await _add_command_context(
        _add_command("POS-NVDA-001"), invocation_handle=_handle_for(record)
    )
    assert ctx["position_asset_type"] == "equity"
    assert ctx["position_side"] == "long"


@pytest.mark.asyncio
async def test_add_options_long_threads_long_instrument_direction() -> None:
    """A LONG options ADD threads a long ``OptionInstrument`` + ``position_side``."""
    record = _options_position()
    ctx = await _add_command_context(
        _add_command("POS-NVDA-OPT-001"), invocation_handle=_handle_for(record)
    )
    assert ctx["position_asset_type"] == "option"
    assert ctx["position_side"] == "long"
    assert ctx["position_option_instrument"].direction == "long"
