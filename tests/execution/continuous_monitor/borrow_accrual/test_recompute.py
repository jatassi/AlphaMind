"""Pure tests for the borrow-accrual recompute kernel (ALP-719).

The kernel runs over typed :class:`PositionRecord` values and explicit
``close_prices`` / ``fee_rates`` mappings. It returns a typed
:class:`AccrualTickResult` carrying the per-position outcomes — updated
:class:`PositionRecord` instances, the activity-log entries to emit, and
the per-tick summary. It raises ``ValueError`` on the first missing close
or missing fee, so the imperative shell can roll the surrounding
transaction back.

These tests are pure: no SQLAlchemy, no asyncio, no clock. The shell-side
behaviors (DB session, InvocationRow insert, atomicity) live in
``test_task.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import PositionId, ThesisId, make_symbol
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.continuous_monitor.borrow_accrual.recompute import (
    compute_tick,
    today_cost_usd,
)
from alphamind.portfolio_state.events.activity_log import (
    BorrowCostAccruedDetail,
    EventGroup,
    EventSource,
    EventType,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

_NOW = datetime(2026, 5, 27, 20, 0, tzinfo=UTC)  # 16:00 ET in summer (UTC-4)
_INV = "inv-20260527T200000Z-deadbeef"


def _short_equity_position(
    *,
    position_id: str,
    ticker: str,
    share_count: float = 100.0,
    accrued: float = 0.0,
    avg_cost: float = 50.0,
    borrow_rate_pct: float = 10.0,
) -> PositionRecord:
    """Build an OPEN SHORT EQUITY position with the four short-only fields stamped."""
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("thesis-1"),
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
        details=EquityPositionDetails(
            ticker=make_symbol(ticker),
            share_count=share_count,
            average_cost_basis_per_share=avg_cost,
            borrow_rate_pct=borrow_rate_pct,
            accrued_borrow_cost_usd=accrued,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=share_count * avg_cost * 0.5,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                fill_price=price(avg_cost),
                fill_quantity=share_count,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _long_equity_position(*, position_id: str, ticker: str) -> PositionRecord:
    """Build an OPEN LONG EQUITY position (excluded from accrual)."""
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("thesis-1"),
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
        details=EquityPositionDetails(
            ticker=make_symbol(ticker),
            share_count=100.0,
            average_cost_basis_per_share=50.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                fill_price=price(50.0),
                fill_quantity=100.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


class TestTodayCostUsd:
    """Pure formula: ``abs(share_count * close_price) * annual_fee_pct / 100 / 252``."""

    def test_canonical_example(self) -> None:
        assert today_cost_usd(
            share_count=100.0, close_price=50.0, annual_fee_pct=10.0
        ) == pytest.approx(100 * 50 * 10 / 100 / 252)

    def test_takes_abs_of_share_count(self) -> None:
        """SHORT positions are stored with a positive ``share_count`` per the
        codec, but the formula uses ``abs`` so a future sign-convention change
        does not break the math."""
        assert today_cost_usd(
            share_count=-100.0, close_price=50.0, annual_fee_pct=10.0
        ) == pytest.approx(today_cost_usd(share_count=100.0, close_price=50.0, annual_fee_pct=10.0))

    def test_zero_fee_returns_zero(self) -> None:
        assert today_cost_usd(share_count=100.0, close_price=50.0, annual_fee_pct=0.0) == 0.0


class TestComputeTickHappyPath:
    """Happy-path tick — one OPEN SHORT position, fee resolved, close resolved."""

    def test_advances_accumulator_by_one_day_cost(self) -> None:
        position = _short_equity_position(position_id="pos-1", ticker="ABCD")
        result = compute_tick(
            positions=(position,),
            close_prices={make_symbol("ABCD"): 50.0},
            fee_rates={make_symbol("ABCD"): 10.0},
            now=_NOW,
            invocation_id=_INV,
        )
        assert len(result.updated_positions) == 1
        updated = result.updated_positions[0]
        assert isinstance(updated.details, EquityPositionDetails)
        expected = 100 * 50 * 10 / 100 / 252
        assert updated.details.accrued_borrow_cost_usd == pytest.approx(expected)

    def test_emits_one_activity_log_entry_with_full_payload(self) -> None:
        position = _short_equity_position(position_id="pos-1", ticker="ABCD", accrued=5.0)
        result = compute_tick(
            positions=(position,),
            close_prices={make_symbol("ABCD"): 50.0},
            fee_rates={make_symbol("ABCD"): 10.0},
            now=_NOW,
            invocation_id=_INV,
        )
        assert len(result.activity_log_entries) == 1
        entry = result.activity_log_entries[0]
        assert entry.event_type == EventType.BORROW_COST_ACCRUED
        assert entry.event_group == EventGroup.CASH_AND_MARGIN
        assert entry.source == EventSource.MARGIN_MONITOR
        assert entry.invocation_id == _INV
        assert entry.position_id == "pos-1"
        assert isinstance(entry.detail, BorrowCostAccruedDetail)
        expected_today = 100 * 50 * 10 / 100 / 252
        assert entry.detail.accrued_amount_usd == money(expected_today)
        assert entry.detail.cumulative_accrued_usd == money(5.0 + expected_today)
        assert entry.detail.annual_fee_pct_used == 10.0
        assert entry.detail.notional_usd_used == money(100 * 50)
        # 2026-05-27 20:00 UTC is 2026-05-27 16:00 ET in summer.
        assert entry.detail.accrual_date == date(2026, 5, 27)

    def test_preserves_thesis_id_and_status_and_history(self) -> None:
        """The codec round-trip preserves identity and history; the kernel only
        touches the accumulator on the details payload."""
        position = _short_equity_position(position_id="pos-1", ticker="ABCD")
        result = compute_tick(
            positions=(position,),
            close_prices={make_symbol("ABCD"): 50.0},
            fee_rates={make_symbol("ABCD"): 10.0},
            now=_NOW,
            invocation_id=_INV,
        )
        updated = result.updated_positions[0]
        assert updated.status == PositionStatus.OPEN
        assert updated.direction == Direction.SHORT
        assert updated.thesis_id == position.thesis_id
        assert updated.execution_history == position.execution_history
        assert updated.entry_timestamp == position.entry_timestamp


class TestComputeTickFiltering:
    """Only OPEN SHORT EQUITY positions accrue; everything else is silently skipped."""

    def test_long_equity_excluded(self) -> None:
        position = _long_equity_position(position_id="pos-1", ticker="ABCD")
        result = compute_tick(
            positions=(position,),
            close_prices={},
            fee_rates={},
            now=_NOW,
            invocation_id=_INV,
        )
        assert result.updated_positions == ()
        assert result.activity_log_entries == ()

    def test_closed_short_equity_excluded(self) -> None:
        """A SHORT EQUITY that closed intraday (status CLOSED) skips this tick."""
        base = _short_equity_position(position_id="pos-1", ticker="ABCD")
        from dataclasses import replace as _replace

        closed = _replace(base, status=PositionStatus.CLOSED, realized_pnl_to_date_usd=-12.34)
        result = compute_tick(
            positions=(closed,),
            close_prices={},
            fee_rates={},
            now=_NOW,
            invocation_id=_INV,
        )
        assert result.updated_positions == ()
        assert result.activity_log_entries == ()

    def test_options_short_excluded(self) -> None:
        """A SHORT OPTIONS position (no equity borrow concept) skips this tick."""
        options_position = PositionRecord(
            position_id=PositionId("pos-1"),
            thesis_id=ThesisId("thesis-1"),
            bracket_id=None,
            status=PositionStatus.OPEN,
            direction=Direction.SHORT,
            entry_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
            details=OptionsPositionDetails(
                underlying_ticker=make_symbol("ABCD"),
                strike_price=50.0,
                expiration_date=date(2026, 6, 19),
                contract_type=OptionContractType.CALL,
                contract_count=1.0,
                contract_multiplier=100.0,
                premium_paid_per_contract=1.0,
                greeks=OptionGreeks(delta=0.5, gamma=0.0, theta=0.0, vega=0.0),
            ),
            execution_history=(
                PositionFill(
                    fill_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                    fill_price=price(1.0),
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
        result = compute_tick(
            positions=(options_position,),
            close_prices={},
            fee_rates={},
            now=_NOW,
            invocation_id=_INV,
        )
        assert result.updated_positions == ()
        assert result.activity_log_entries == ()


class TestComputeTickFailFast:
    """A missing close or missing fee for any in-scope ticker raises ``ValueError``.

    The shell rolls the whole tick back on the raise.
    """

    def test_missing_close_price_raises_value_error_naming_ticker(self) -> None:
        position = _short_equity_position(position_id="pos-1", ticker="WXYZ")
        with pytest.raises(ValueError, match="WXYZ"):
            compute_tick(
                positions=(position,),
                close_prices={},  # no close for WXYZ
                fee_rates={make_symbol("WXYZ"): 10.0},
                now=_NOW,
                invocation_id=_INV,
            )

    def test_missing_fee_rate_raises_value_error_naming_ticker(self) -> None:
        position = _short_equity_position(position_id="pos-1", ticker="EFGH")
        with pytest.raises(ValueError, match="EFGH"):
            compute_tick(
                positions=(position,),
                close_prices={make_symbol("EFGH"): 50.0},
                fee_rates={make_symbol("EFGH"): None},  # resolver miss
                now=_NOW,
                invocation_id=_INV,
            )

    def test_resolver_miss_on_one_of_two_positions_raises_and_drops_other(self) -> None:
        """The kernel raises on first miss; the shell's atomic transaction is
        the mechanism that rolls back the other position."""
        ok = _short_equity_position(position_id="pos-1", ticker="ABCD")
        bad = _short_equity_position(position_id="pos-2", ticker="EFGH")
        with pytest.raises(ValueError, match="EFGH"):
            compute_tick(
                positions=(ok, bad),
                close_prices={make_symbol("ABCD"): 50.0, make_symbol("EFGH"): 50.0},
                fee_rates={make_symbol("ABCD"): 10.0, make_symbol("EFGH"): None},
                now=_NOW,
                invocation_id=_INV,
            )


class TestComputeTickMultiPosition:
    """Multiple OPEN SHORT positions each yield their own entry + update."""

    def test_three_positions_yield_three_updates_and_entries(self) -> None:
        positions = (
            _short_equity_position(position_id="pos-1", ticker="ABCD", share_count=100.0),
            _short_equity_position(position_id="pos-2", ticker="EFGH", share_count=200.0),
            _short_equity_position(position_id="pos-3", ticker="IJKL", share_count=50.0),
        )
        result = compute_tick(
            positions=positions,
            close_prices={
                make_symbol("ABCD"): 50.0,
                make_symbol("EFGH"): 25.0,
                make_symbol("IJKL"): 100.0,
            },
            fee_rates={
                make_symbol("ABCD"): 10.0,
                make_symbol("EFGH"): 5.0,
                make_symbol("IJKL"): 20.0,
            },
            now=_NOW,
            invocation_id=_INV,
        )
        assert len(result.updated_positions) == 3
        assert len(result.activity_log_entries) == 3
        # Per-position accumulators advance by their own computed amount.
        expected = [
            100 * 50 * 10 / 100 / 252,
            200 * 25 * 5 / 100 / 252,
            50 * 100 * 20 / 100 / 252,
        ]
        for updated, exp in zip(result.updated_positions, expected, strict=True):
            assert isinstance(updated.details, EquityPositionDetails)
            assert updated.details.accrued_borrow_cost_usd == pytest.approx(exp)
