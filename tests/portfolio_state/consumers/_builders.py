"""Shared low-level record and view builders for portfolio_state/consumers/ tests.

Hoisted from the five consumer test files to centralize record-shape definitions
(ALP-788). All builders here produce data identical to the prior per-file copies.

These are pure data builders (no test logic). Import explicitly in the test modules
that need them (mirrors the pattern in tests/portfolio_state/_fixtures.py).

ALP-821: the record builders that were byte-identical to ``_view_builders`` are now
re-exported from there unchanged (``_make_bracket``, ``_make_pending_order``,
``_make_risk_budget``, and ``_make_active_risk_parameters`` under this suite's
``_make_active_risk_params`` name). ``_make_cash_ledger`` / ``_make_drawdown_state``
delegate to the shared builders while pinning the consumer-suite values (e.g.
``true_deployable_capital_usd=44000`` is asserted by test_analyst).
``_make_open_position`` / ``_make_pending_position`` stay local because they encode
AAPL/GOOG fixture values the consumer assertions depend on, which differ from the
NVDA-based view fixtures — merging would force assertion changes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView

from .._view_builders import (
    _make_active_risk_parameters as _make_active_risk_params,
)
from .._view_builders import (
    _make_bracket,
    _make_pending_order,
    _make_risk_budget,
)
from .._view_builders import (
    _make_cash_ledger as _shared_make_cash_ledger,
)
from .._view_builders import (
    _make_drawdown_state as _shared_make_drawdown_state,
)

__all__ = [
    "_make_active_risk_params",
    "_make_bracket",
    "_make_cash_ledger",
    "_make_drawdown_state",
    "_make_fill",
    "_make_open_position",
    "_make_pending_order",
    "_make_pending_position",
    "_make_risk_budget",
]

# ---------------------------------------------------------------------------
# Shared timestamps (values match the per-file _T0 across all consumer tests)
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Consumer-specific builders (kept local — values are asserted by the consumer
# suites and differ from the view-side fixtures)
# ---------------------------------------------------------------------------


def _make_fill(at_price: float = 150.0) -> PositionFill:
    return PositionFill(
        fill_timestamp=_T0,
        fill_price=price(at_price),
        fill_quantity=100.0,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )


def _make_open_position(pos_id: str = "POS-001", ticker: str = "AAPL") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=100.0,
        average_cost_basis_per_share=150.0,
    )
    record = PositionRecord(
        position_id=PositionId(pos_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_T0,
        details=equity,
        execution_history=(_make_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(15500.0),
        unrealized_pnl_usd=signed_money(500.0),
        unrealized_pnl_pct=3.33,
        position_weight_pct=10.0,
        position_age_hours=4.0,
        notional_exposure_usd=money(15000.0),
        delta_adjusted_exposure_usd=signed_money(15000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_pending_position(pos_id: str = "POS-PEND") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol("GOOG"),
        share_count=10.0,
        average_cost_basis_per_share=2800.0,
    )
    record = PositionRecord(
        position_id=PositionId(pos_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=equity,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(0.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=0.0,
        position_age_hours=0.0,
        notional_exposure_usd=money(0.0),
        delta_adjusted_exposure_usd=signed_money(0.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


# ---------------------------------------------------------------------------
# Thin wrappers over the shared builders (ALP-821): preserve the consumer-suite
# values while keeping the CashLedger/DrawdownState literals defined once in
# _view_builders.
# ---------------------------------------------------------------------------


def _make_cash_ledger() -> CashLedger:
    return _shared_make_cash_ledger(
        current_cash_usd=50000.0,
        settled_cash_usd=48000.0,
        reserved_capital_usd=2000.0,
        available_buying_power_usd=46000.0,
        margin_held_usd=0.0,
        cash_pct_of_portfolio=50.0,
        true_deployable_capital_usd=44000.0,
        regt_excess_trailing_30d_usd=1000.0,
        regt_excess_trailing_90d_usd=3000.0,
        regt_excess_lifetime_usd=10000.0,
    )


def _make_drawdown_state() -> DrawdownState:
    return _shared_make_drawdown_state(
        current_drawdown_pct=2.0,
        equity_high_water_mark_usd=110000.0,
        drawdown_duration_hours=8.0,
        lifetime_max_drawdown_pct=5.0,
        intraday_drawdown_pct=0.5,
    )
