"""Corporate-actions end-to-end verification (ALP-416).

Operator entry point (``uv run python -m alphamind.scripts.verify_corporate_actions``).
Exercises the corporate-actions integration pipeline end-to-end against a
freshly-migrated SQLite DB across the per-action-type matrix from
``docs/design/05-execution-layer/corporate-actions.md``.

For each of the nine ``CorporateActionType`` members, the script seeds the
matching local position(s) + bracket + thesis cluster, drains a synthetic
``CorporateActionActivity`` through ``process_unprocessed_fills`` inside an
``InvocationContext``, and asserts the post-state matches the design's matrix
(quantity, cost basis, ticker, status, cash impact, and flag). A tenth
deliberate-divergence variant supplies a mismatched ``PositionSnapshot`` so
the reconciliation step emits one ``RECONCILIATION_ALERT``.

No SDK invocation; no live broker contact. Sub-second runtime against a fresh
on-disk DB.

Usage::

    uv run python -m alphamind.scripts.verify_corporate_actions [--db-path PATH]

See ``scripts/RUNBOOK_corporate_actions.md`` for the operator runbook.
"""
# mypy: disable-error-code="arg-type,call-arg,dict-item,misc,no-untyped-def,no-untyped-call,unused-ignore,no-any-return,var-annotated"

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price
from alphamind._kernel.regime import RiskZone
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.corporate_actions.types import CorporateActionActivity
from alphamind.execution.write_paths.phase1 import (
    process_unprocessed_fills,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.events.activity_log import (
    CorporateActionType,
    EventType,
)
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
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
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
    RealizedVolEntry,
)
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.context import (
    InvocationContext,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    CorporateActionLedgerStatus,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

__all__ = [
    "ActionRowResult",
    "VerifyResult",
    "main",
    "run_verify",
]


_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_PROCESS_ID = "verify-corporate-actions-proc"
_INVOCATION_ID = "verify-corporate-actions-inv"
_INITIAL_CASH = 100_000.0

# Tickers — one per scenario so reconciliation matching by symbol stays unambiguous.
_TICKER_SPLIT = "SPL"
_TICKER_REV_SPLIT = "RVS"
_TICKER_STOCK_DIV = "STD"
_TICKER_CD_LONG = "CDL"
_TICKER_CD_SHORT = "CDS"
_TICKER_CASH_MERGER = "CMR"
_TICKER_STOCK_MERGER_OLD = "SMA"
_TICKER_STOCK_MERGER_NEW = "ACQ"
_TICKER_SPIN_OFF_PARENT = "PAR"
_TICKER_SPIN_OFF_CHILD = "CHL"
_TICKER_SYMBOL_CHANGE_OLD = "OLD"
_TICKER_SYMBOL_CHANGE_NEW = "NEW"
_TICKER_STRATEGY = "STR"

# Position IDs — one per CA + the seeded options-and-strategy "category" rows.
_POS_SPLIT = "pos-split"
_POS_REV_SPLIT = "pos-rev-split"
_POS_STOCK_DIV = "pos-stock-div"
_POS_CD_LONG = "pos-cd-long"
_POS_CD_SHORT = "pos-cd-short"
_POS_CASH_MERGER = "pos-cash-merger"
_POS_STOCK_MERGER = "pos-stock-merger"
_POS_SPIN_OFF = "pos-spin-off"
_POS_SYMBOL_CHANGE = "pos-symbol-change"  # options-long
_POS_STRATEGY = "pos-strategy"  # vertical spread; no CA targets it

# Per-CA initial state.
_SPLIT_PRE_QTY = 10.0
_SPLIT_PRE_BASIS = 150.0
_SPLIT_RATIO = 4.0  # 4:1 forward split.

_REV_SPLIT_PRE_QTY = 100.0
_REV_SPLIT_PRE_BASIS = 5.0
_REV_SPLIT_RATIO = 10.0  # 1:10 reverse split.
_REV_SPLIT_FRACTIONAL_CASH = 25.0  # synthetic residual cash-out.

_STOCK_DIV_PRE_QTY = 20.0
_STOCK_DIV_PRE_BASIS = 80.0
_STOCK_DIV_RATE = 0.10  # 10% stock dividend.

_CD_LONG_PRE_QTY = 10.0
_CD_LONG_PRE_BASIS = 200.0
_CD_LONG_CASH_CREDIT = 50.0

_CD_SHORT_PRE_QTY = 10.0
_CD_SHORT_PRE_BASIS = 100.0
_CD_SHORT_CASH_DEBIT = -50.0  # signed; debit

_CASH_MERGER_PRE_QTY = 10.0
_CASH_MERGER_PRE_BASIS = 100.0
_CASH_MERGER_PROCEEDS = 1100.0  # cash credit on close.

_STOCK_MERGER_PRE_QTY = 10.0
_STOCK_MERGER_PRE_BASIS = 100.0
_STOCK_MERGER_NEW_QTY = 6.0  # 0.6 acquirer share per acquiree share.
_STOCK_MERGER_NEW_BASIS = 166.67
_STOCK_MERGER_CASH_PARTIAL = 100.0  # cash-and-stock partial.

_SPIN_OFF_PARENT_QTY = 100.0
_SPIN_OFF_PARENT_PRE_BASIS = 200.0
_SPIN_OFF_PARENT_POST_BASIS = 160.0
_SPIN_OFF_CHILD_QTY = 50.0
_SPIN_OFF_CHILD_BASIS = 40.0

_SYMBOL_CHANGE_PRE_CONTRACTS = 5.0
_SYMBOL_CHANGE_PRE_PREMIUM = 250.0

_STRATEGY_LEG_CONTRACTS = 5.0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRowResult:
    """One per-action-type-row outcome — pass/fail + named label + diagnostic."""

    label: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class VerifyResult:
    """Aggregated verification result with one row per action type plus reconciliation."""

    action_rows: tuple[ActionRowResult, ...]
    reconciliation_alerts_emitted: int
    reconciliation_alerts_expected: int
    reconciliation_summary_ok: bool

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.action_rows) and self.reconciliation_summary_ok


# ---------------------------------------------------------------------------
# Substrate / fixture builders
# ---------------------------------------------------------------------------


def _process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="verify-corporate-actions",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/verify-corporate-actions/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="verify-script",
    )


def _invocation_record() -> InvocationRecord:
    return InvocationRecord(
        invocation_id=_INVOCATION_ID,
        process_lifetime_id=_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="verify_corporate_actions.py",
        trigger_reason="operator-triggered verification",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/verify-corporate-actions/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/verify-corporate-actions/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/verify-corporate-actions",
            "invocation_provenance_root": "/tmp/verify-corporate-actions",
        }
    )


def _market_inputs() -> MarketInputs:
    """Fixture ``MarketInputs`` for the Phase 1 wedge in this verify script.

    Story 06a / ALP-428 added a required ``market_inputs`` argument to
    ``process_unprocessed_fills``. The script seeds many tickers (one per CA
    action type plus the strategy underlying); each gets a positive scalar
    so the wedge can price every open position. The strategy ticker also
    flows through the IV provider, but the realized-vol fallback covers
    arbitrary strikes/expirations the Black-Scholes path may consult.
    """
    underlying_prices = {
        ticker: 100.0
        for ticker in (
            _TICKER_SPLIT,
            _TICKER_REV_SPLIT,
            _TICKER_STOCK_DIV,
            _TICKER_CD_LONG,
            _TICKER_CD_SHORT,
            _TICKER_CASH_MERGER,
            _TICKER_STOCK_MERGER_OLD,
            _TICKER_STOCK_MERGER_NEW,
            _TICKER_SPIN_OFF_PARENT,
            _TICKER_SPIN_OFF_CHILD,
            _TICKER_SYMBOL_CHANGE_OLD,
            _TICKER_SYMBOL_CHANGE_NEW,
            _TICKER_STRATEGY,
        )
    }
    realized_vol = {
        _TICKER_STRATEGY: RealizedVolEntry(
            underlying=_TICKER_STRATEGY,
            trailing_30d_realized_vol=0.30,
        )
    }
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=0.0425,
        iv_provider=FixtureIvProvider(surface={}, realized_vol=realized_vol),
        as_of=_NOW,
    )


def _cash_ledger() -> CashLedger:
    return CashLedger(
        current_cash_usd=_INITIAL_CASH,
        settled_cash_usd=_INITIAL_CASH,
        reserved_capital_usd=0.0,
        available_buying_power_usd=_INITIAL_CASH,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )


def _drawdown_state() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=_INITIAL_CASH,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _equity_long_position(
    position_id: str,
    *,
    ticker: str,
    share_count: float,
    average_cost_basis_per_share: float,
    thesis_id: str,
    bracket_id: str,
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=average_cost_basis_per_share,
            fill_quantity=share_count,
            slippage=0.0,
            fees=0.0,
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id),
        bracket_id=BracketId(bracket_id),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _equity_short_position(
    position_id: str,
    *,
    ticker: str,
    share_count: float,
    average_cost_basis_per_share: float,
    thesis_id: str,
    bracket_id: str,
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
        borrow_rate_pct=0.025,
        locate_status=LocateStatus.LOCATED,
        margin_held_usd=share_count * average_cost_basis_per_share * 0.5,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=average_cost_basis_per_share,
            fill_quantity=share_count,
            slippage=0.0,
            fees=0.0,
        ),
    )
    return PositionRecord(
        position_id=position_id,
        thesis_id=thesis_id,
        bracket_id=bracket_id,
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _options_long_position(
    position_id: str,
    *,
    underlying_ticker: str,
    contract_count: float,
    premium_paid_per_contract: float,
    thesis_id: str,
    bracket_id: str,
) -> PositionRecord:
    details = OptionsPositionDetails(
        underlying_ticker=Symbol(underlying_ticker),
        strike_price=150.0,
        expiration_date=date(2026, 9, 18),
        contract_type=OptionContractType.CALL,
        contract_count=contract_count,
        contract_multiplier=100.0,
        premium_paid_per_contract=premium_paid_per_contract,
        greeks=OptionGreeks(delta=0.45, gamma=0.02, theta=-0.05, vega=0.10),
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=premium_paid_per_contract / 100.0,
            fill_quantity=contract_count,
            slippage=0.0,
            fees=0.0,
        ),
    )
    return PositionRecord(
        position_id=position_id,
        thesis_id=thesis_id,
        bracket_id=bracket_id,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(
    position_id: str,
    *,
    underlying_ticker: str,
    thesis_id: str,
    bracket_id: str,
) -> PositionRecord:
    leg_long = StrategyLeg(
        leg_id=f"{position_id}-leg-long",
        direction=Direction.LONG,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol(underlying_ticker),
            strike_price=150.0,
            expiration_date=date(2026, 9, 18),
            contract_type=OptionContractType.CALL,
            contract_count=_STRATEGY_LEG_CONTRACTS,
            contract_multiplier=100.0,
            premium_paid_per_contract=250.0,
            greeks=OptionGreeks(delta=0.45, gamma=0.02, theta=-0.05, vega=0.10),
        ),
    )
    leg_short = StrategyLeg(
        leg_id=f"{position_id}-leg-short",
        direction=Direction.SHORT,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol(underlying_ticker),
            strike_price=160.0,
            expiration_date=date(2026, 9, 18),
            contract_type=OptionContractType.CALL,
            contract_count=_STRATEGY_LEG_CONTRACTS,
            contract_multiplier=100.0,
            premium_paid_per_contract=120.0,
            greeks=OptionGreeks(delta=0.25, gamma=0.02, theta=-0.04, vega=0.08),
        ),
    )
    details = StrategyPositionDetails(
        strategy_type_label="vertical-call-spread",
        legs=(leg_long, leg_short),
        net_premium_usd=650.0,
        max_profit_usd=4350.0,
        max_loss_usd=650.0,
        breakeven_levels=(151.30,),
        strategy_greeks=OptionGreeks(delta=0.20, gamma=0.01, theta=-0.02, vega=0.05),
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=1.30,
            fill_quantity=_STRATEGY_LEG_CONTRACTS,
            slippage=0.0,
            fees=0.0,
        ),
    )
    return PositionRecord(
        position_id=position_id,
        thesis_id=thesis_id,
        bracket_id=bracket_id,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _entry_order(order_id: str, *, bracket_id: str, position_id: str, ticker: str) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(ticker)),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(f"alp-{order_id}",),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def _stop_order(order_id: str, *, bracket_id: str, ticker: str) -> OrderRecord:
    """Protective stop sibling order referenced by the bracket's protective leg.

    The bracket leg's ``order_id`` points at this row; without it, the FK
    enforcement migration would reject the leg at COMMIT.
    """
    return OrderRecord(
        order_id=order_id,
        position_id=None,
        bracket_id=bracket_id,
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(ticker)),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(stop_trigger_price=0.01),
        quantity=10.0,
        duration=OrderDuration.GTC,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(f"alp-{order_id}",),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def _bracket(
    bracket_id: str, *, position_id: str, ticker: str, entry_order_id: str
) -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(underlying_ticker=Symbol(ticker), threshold_usd=0.01, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId(entry_order_id),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _thesis(thesis_id: str, *, position_id: str, ticker: str) -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=ThesisId(thesis_id),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=ticker,
            narrative=f"{ct.value} narrative",
            key_assumptions=(KeyAssumption(text="Catalyst", outcome=None),),
            generation_timestamp=_NOW - timedelta(hours=4),
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    generation_at = _NOW - timedelta(hours=4)
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary=f"{ticker} momentum",
        key_catalyst="Catalyst",
        position_size_rationale="Sized at 5%",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=generation_at,
        time_expectation_hours=24.0,
        age_hours=4.0,
        expected_resolution_at=generation_at + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


@dataclass(frozen=True)
class _SeedScenario:
    """One position + its FK cluster (thesis, bracket, entry order, stop order)."""

    position: PositionRecord
    thesis: ThesisRecord
    bracket: BracketRecord
    entry_order: OrderRecord
    stop_order: OrderRecord


def _build_scenario(
    *,
    position_id: str,
    ticker: str,
    position: PositionRecord,
) -> _SeedScenario:
    thesis_id = f"thesis-{position_id}"
    bracket_id = f"brk-{position_id}"
    entry_order_id = f"ord-entry-{position_id}"
    stop_order_id = f"{bracket_id}-ord-stop"
    return _SeedScenario(
        position=position,
        thesis=_thesis(thesis_id, position_id=position_id, ticker=ticker),
        bracket=_bracket(
            bracket_id,
            position_id=position_id,
            ticker=ticker,
            entry_order_id=entry_order_id,
        ),
        entry_order=_entry_order(
            entry_order_id, bracket_id=bracket_id, position_id=position_id, ticker=ticker
        ),
        stop_order=_stop_order(stop_order_id, bracket_id=bracket_id, ticker=ticker),
    )


def _all_scenarios() -> tuple[_SeedScenario, ...]:
    """Build the ten seeded position scenarios — nine CA targets + the strategy filler."""
    return (
        _build_scenario(
            position_id=_POS_SPLIT,
            ticker=_TICKER_SPLIT,
            position=_equity_long_position(
                _POS_SPLIT,
                ticker=_TICKER_SPLIT,
                share_count=_SPLIT_PRE_QTY,
                average_cost_basis_per_share=_SPLIT_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_SPLIT}"),
                bracket_id=BracketId(f"brk-{_POS_SPLIT}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_REV_SPLIT,
            ticker=_TICKER_REV_SPLIT,
            position=_equity_long_position(
                _POS_REV_SPLIT,
                ticker=_TICKER_REV_SPLIT,
                share_count=_REV_SPLIT_PRE_QTY,
                average_cost_basis_per_share=_REV_SPLIT_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_REV_SPLIT}"),
                bracket_id=BracketId(f"brk-{_POS_REV_SPLIT}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_STOCK_DIV,
            ticker=_TICKER_STOCK_DIV,
            position=_equity_long_position(
                _POS_STOCK_DIV,
                ticker=_TICKER_STOCK_DIV,
                share_count=_STOCK_DIV_PRE_QTY,
                average_cost_basis_per_share=_STOCK_DIV_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_STOCK_DIV}"),
                bracket_id=BracketId(f"brk-{_POS_STOCK_DIV}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_CD_LONG,
            ticker=_TICKER_CD_LONG,
            position=_equity_long_position(
                _POS_CD_LONG,
                ticker=_TICKER_CD_LONG,
                share_count=_CD_LONG_PRE_QTY,
                average_cost_basis_per_share=_CD_LONG_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_CD_LONG}"),
                bracket_id=BracketId(f"brk-{_POS_CD_LONG}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_CD_SHORT,
            ticker=_TICKER_CD_SHORT,
            position=_equity_short_position(
                _POS_CD_SHORT,
                ticker=_TICKER_CD_SHORT,
                share_count=_CD_SHORT_PRE_QTY,
                average_cost_basis_per_share=_CD_SHORT_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_CD_SHORT}"),
                bracket_id=BracketId(f"brk-{_POS_CD_SHORT}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_CASH_MERGER,
            ticker=_TICKER_CASH_MERGER,
            position=_equity_long_position(
                _POS_CASH_MERGER,
                ticker=_TICKER_CASH_MERGER,
                share_count=_CASH_MERGER_PRE_QTY,
                average_cost_basis_per_share=_CASH_MERGER_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_CASH_MERGER}"),
                bracket_id=BracketId(f"brk-{_POS_CASH_MERGER}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_STOCK_MERGER,
            ticker=_TICKER_STOCK_MERGER_OLD,
            position=_equity_long_position(
                _POS_STOCK_MERGER,
                ticker=_TICKER_STOCK_MERGER_OLD,
                share_count=_STOCK_MERGER_PRE_QTY,
                average_cost_basis_per_share=_STOCK_MERGER_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_STOCK_MERGER}"),
                bracket_id=BracketId(f"brk-{_POS_STOCK_MERGER}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_SPIN_OFF,
            ticker=_TICKER_SPIN_OFF_PARENT,
            position=_equity_long_position(
                _POS_SPIN_OFF,
                ticker=_TICKER_SPIN_OFF_PARENT,
                share_count=_SPIN_OFF_PARENT_QTY,
                average_cost_basis_per_share=_SPIN_OFF_PARENT_PRE_BASIS,
                thesis_id=ThesisId(f"thesis-{_POS_SPIN_OFF}"),
                bracket_id=BracketId(f"brk-{_POS_SPIN_OFF}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_SYMBOL_CHANGE,
            ticker=_TICKER_SYMBOL_CHANGE_OLD,
            position=_options_long_position(
                _POS_SYMBOL_CHANGE,
                underlying_ticker=_TICKER_SYMBOL_CHANGE_OLD,
                contract_count=_SYMBOL_CHANGE_PRE_CONTRACTS,
                premium_paid_per_contract=_SYMBOL_CHANGE_PRE_PREMIUM,
                thesis_id=ThesisId(f"thesis-{_POS_SYMBOL_CHANGE}"),
                bracket_id=BracketId(f"brk-{_POS_SYMBOL_CHANGE}"),
            ),
        ),
        _build_scenario(
            position_id=_POS_STRATEGY,
            ticker=_TICKER_STRATEGY,
            position=_strategy_position(
                _POS_STRATEGY,
                underlying_ticker=_TICKER_STRATEGY,
                thesis_id=ThesisId(f"thesis-{_POS_STRATEGY}"),
                bracket_id=BracketId(f"brk-{_POS_STRATEGY}"),
            ),
        ),
    )


def _ca_activities() -> tuple[CorporateActionActivity, ...]:
    """Build the nine synthetic ``CorporateActionActivity`` records, one per type."""
    ts = _NOW - timedelta(minutes=5)
    return (
        CorporateActionActivity(
            alpaca_activity_id="ca-split-1",
            action_type=CorporateActionType.SPLIT,
            ticker=_TICKER_SPLIT,
            new_ticker=None,
            ratio_or_amount=_SPLIT_RATIO,
            position_id=_POS_SPLIT,
            signed_cash_impact_usd=0.0,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-rev-split-1",
            action_type=CorporateActionType.REVERSE_SPLIT,
            ticker=_TICKER_REV_SPLIT,
            new_ticker=None,
            ratio_or_amount=_REV_SPLIT_RATIO,
            position_id=_POS_REV_SPLIT,
            signed_cash_impact_usd=_REV_SPLIT_FRACTIONAL_CASH,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-stock-div-1",
            action_type=CorporateActionType.STOCK_DIVIDEND,
            ticker=_TICKER_STOCK_DIV,
            new_ticker=None,
            ratio_or_amount=_STOCK_DIV_RATE,
            position_id=_POS_STOCK_DIV,
            signed_cash_impact_usd=0.0,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-cd-long-1",
            action_type=CorporateActionType.CASH_DIVIDEND_LONG,
            ticker=_TICKER_CD_LONG,
            new_ticker=None,
            ratio_or_amount=5.0,
            position_id=_POS_CD_LONG,
            signed_cash_impact_usd=_CD_LONG_CASH_CREDIT,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-cd-short-1",
            action_type=CorporateActionType.CASH_DIVIDEND_SHORT,
            ticker=_TICKER_CD_SHORT,
            new_ticker=None,
            ratio_or_amount=5.0,
            position_id=_POS_CD_SHORT,
            signed_cash_impact_usd=_CD_SHORT_CASH_DEBIT,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-cash-merger-1",
            action_type=CorporateActionType.CASH_MERGER,
            ticker=_TICKER_CASH_MERGER,
            new_ticker=None,
            ratio_or_amount=0.0,
            position_id=_POS_CASH_MERGER,
            signed_cash_impact_usd=_CASH_MERGER_PROCEEDS,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-stock-merger-1",
            action_type=CorporateActionType.STOCK_MERGER,
            ticker=_TICKER_STOCK_MERGER_OLD,
            new_ticker=_TICKER_STOCK_MERGER_NEW,
            ratio_or_amount=0.6,
            position_id=_POS_STOCK_MERGER,
            signed_cash_impact_usd=_STOCK_MERGER_CASH_PARTIAL,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-spin-off-1",
            action_type=CorporateActionType.SPIN_OFF,
            ticker=_TICKER_SPIN_OFF_PARENT,
            new_ticker=_TICKER_SPIN_OFF_CHILD,
            ratio_or_amount=0.5,
            position_id=_POS_SPIN_OFF,
            signed_cash_impact_usd=0.0,
            transaction_time=ts,
        ),
        CorporateActionActivity(
            alpaca_activity_id="ca-symbol-change-1",
            action_type=CorporateActionType.SYMBOL_CHANGE,
            ticker=_TICKER_SYMBOL_CHANGE_OLD,
            new_ticker=_TICKER_SYMBOL_CHANGE_NEW,
            ratio_or_amount=0.0,
            position_id=_POS_SYMBOL_CHANGE,
            signed_cash_impact_usd=0.0,
            transaction_time=ts,
        ),
    )


def _equity_snapshot(symbol: str, qty: float, avg_entry_price: float) -> PositionSnapshot:
    # ALP-462 — PositionSnapshot fields are Price/Money; wrap fixture floats.
    px = price(str(avg_entry_price))
    notional = money(str(qty * avg_entry_price))
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=qty,
        avg_entry_price=px,
        market_value=notional,
        cost_basis=notional,
        unrealized_pl=money("0"),
        unrealized_plpc=0.0,
        current_price=px,
        side="long",
    )


def _equity_short_snapshot(symbol: str, qty: float, avg_entry_price: float) -> PositionSnapshot:
    px = price(str(avg_entry_price))
    notional = money(str(qty * avg_entry_price))
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=qty,
        avg_entry_price=px,
        market_value=notional,
        cost_basis=notional,
        unrealized_pl=money("0"),
        unrealized_plpc=0.0,
        current_price=px,
        side="short",
    )


def _options_snapshot(symbol: str, qty: float, avg_entry_price: float) -> PositionSnapshot:
    px = price(str(avg_entry_price))
    notional = money(str(qty * avg_entry_price * 100.0))
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_option",
        qty=qty,
        avg_entry_price=px,
        market_value=notional,
        cost_basis=notional,
        unrealized_pl=money("0"),
        unrealized_plpc=0.0,
        current_price=px,
        side="long",
    )


def _alpaca_positions_aligned() -> tuple[PositionSnapshot, ...]:
    """Tuple of ``PositionSnapshot`` matching the local post-CA state.

    Each snapshot mirrors what Alpaca would report after applying the CA on
    its end. Handlers consult this via :class:`_SnapshotLookup` (for STOCK_MERGER,
    SPIN_OFF, and options branches); the reconciler compares against the
    post-CA local state and should find no divergence (alert_count == 0).

    The cash-dividend-long ticker (``_TICKER_CD_LONG``) is deliberately
    seeded with an off-by-one share count so the reconciler emits exactly one
    alert. The remaining snapshots are aligned.
    """
    return (
        # SPLIT: post-CA 40 shares (qty * ratio), basis 37.5 (basis / ratio).
        _equity_snapshot(_TICKER_SPLIT, qty=_SPLIT_PRE_QTY * _SPLIT_RATIO, avg_entry_price=37.5),
        # REVERSE_SPLIT: post-CA 10 shares (qty / ratio), basis 50 (basis * ratio).
        _equity_snapshot(
            _TICKER_REV_SPLIT,
            qty=_REV_SPLIT_PRE_QTY / _REV_SPLIT_RATIO,
            avg_entry_price=_REV_SPLIT_PRE_BASIS * _REV_SPLIT_RATIO,
        ),
        # STOCK_DIVIDEND: post-CA 22 shares (qty * (1+rate)).
        _equity_snapshot(
            _TICKER_STOCK_DIV,
            qty=_STOCK_DIV_PRE_QTY * (1.0 + _STOCK_DIV_RATE),
            avg_entry_price=_STOCK_DIV_PRE_BASIS / (1.0 + _STOCK_DIV_RATE),
        ),
        # CASH_DIVIDEND_LONG: deliberate divergence — local stays at 10, Alpaca claims 11.
        _equity_snapshot(
            _TICKER_CD_LONG, qty=_CD_LONG_PRE_QTY + 1.0, avg_entry_price=_CD_LONG_PRE_BASIS
        ),
        # CASH_DIVIDEND_SHORT: short-side; qty unchanged.
        _equity_short_snapshot(
            _TICKER_CD_SHORT, qty=_CD_SHORT_PRE_QTY, avg_entry_price=_CD_SHORT_PRE_BASIS
        ),
        # CASH_MERGER: position closes — Alpaca won't report it (and reconciler skips
        # CLOSED positions; the merged ticker is omitted from the snapshot).
        # STOCK_MERGER: handler reads acquirer ticker — provide that. Old ticker is
        # gone in Alpaca; provide the acquirer position so the handler can read it.
        _equity_snapshot(
            _TICKER_STOCK_MERGER_NEW,
            qty=_STOCK_MERGER_NEW_QTY,
            avg_entry_price=_STOCK_MERGER_NEW_BASIS,
        ),
        # SPIN_OFF: parent post-state + child position.
        _equity_snapshot(
            _TICKER_SPIN_OFF_PARENT,
            qty=_SPIN_OFF_PARENT_QTY,
            avg_entry_price=_SPIN_OFF_PARENT_POST_BASIS,
        ),
        _equity_snapshot(
            _TICKER_SPIN_OFF_CHILD,
            qty=_SPIN_OFF_CHILD_QTY,
            avg_entry_price=_SPIN_OFF_CHILD_BASIS,
        ),
        # SYMBOL_CHANGE (options): post-rename underlying. Lookup not consulted
        # for ticker-only mutation, but reconcile sees the new ticker.
        _options_snapshot(
            _TICKER_SYMBOL_CHANGE_NEW, qty=_SYMBOL_CHANGE_PRE_CONTRACTS, avg_entry_price=2.5
        ),
        # Strategy position: reconciler skips strategy positions at this layer
        # (continuous monitor leg-by-leg comparison). Omit from snapshot.
    )


def _alpaca_account_aligned() -> TradeAccountSnapshot:
    """Post-CA cash balance that matches what the local cash ledger should end at.

    Starting cash ``_INITIAL_CASH`` and the per-CA cash movements:

    * ``+ _REV_SPLIT_FRACTIONAL_CASH`` (REVERSE_SPLIT residual)
    * ``+ _CD_LONG_CASH_CREDIT`` (CASH_DIVIDEND_LONG)
    * ``+ _CD_SHORT_CASH_DEBIT`` (negative — CASH_DIVIDEND_SHORT obligation)
    * ``+ _CASH_MERGER_PROCEEDS``
    * ``+ _STOCK_MERGER_CASH_PARTIAL``
    """
    expected_cash = (
        _INITIAL_CASH
        + _REV_SPLIT_FRACTIONAL_CASH
        + _CD_LONG_CASH_CREDIT
        + _CD_SHORT_CASH_DEBIT
        + _CASH_MERGER_PROCEEDS
        + _STOCK_MERGER_CASH_PARTIAL
    )
    # ALP-462 — TradeAccountSnapshot fields are Money; wrap fixture floats.
    return TradeAccountSnapshot(
        account_id="alp-account-1",
        cash=money(str(expected_cash)),
        equity=money(str(expected_cash)),
        buying_power=money("200000"),
        regt_buying_power=money("200000"),
        daytrading_buying_power=money("200000"),
        maintenance_margin=money("0"),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


# ---------------------------------------------------------------------------
# DB seeding
# ---------------------------------------------------------------------------


async def _seed_substrate_and_singletons(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime_record()))
        await sess.flush()
        sess.add(invocation_record_to_row(_invocation_record()))
        sess.add(cash_ledger_record_to_row(_cash_ledger(), last_updated_at=_NOW))
        sess.add(drawdown_state_record_to_row(_drawdown_state(), last_updated_at=_NOW))
        await sess.commit()


async def _seed_scenarios(
    factory: async_sessionmaker[AsyncSession], scenarios: Sequence[_SeedScenario]
) -> None:
    """Insert each scenario's position + thesis + bracket + entry order + stop order.

    All four cyclically-referencing rows must land in one transaction so the
    deferred FKs check cleanly at COMMIT.
    """
    async with factory() as sess:
        for sc in scenarios:
            thesis_row, component_rows = thesis_record_to_rows(sc.thesis)
            bracket_row, leg_rows = bracket_record_to_rows(sc.bracket)

            sess.add(position_record_to_row(sc.position))
            sess.add(thesis_row)
            for crow in component_rows:
                sess.add(crow)
            sess.add(order_record_to_row(sc.entry_order))
            sess.add(order_record_to_row(sc.stop_order))
            sess.add(bracket_row)
            await sess.flush()
            for lrow in leg_rows:
                sess.add(lrow)
        await sess.commit()


def _open_async_factory(
    db_path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(engine)
    return engine, factory


# ---------------------------------------------------------------------------
# Per-action-type validators
# ---------------------------------------------------------------------------


async def _read_position(
    factory: async_sessionmaker[AsyncSession], position_id: str
) -> PositionRecord:
    async with factory() as sess:
        row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == position_id))
        ).scalar_one()
        return position_row_to_record(row)


async def _read_ledger_row(
    factory: async_sessionmaker[AsyncSession], alpaca_activity_id: str
) -> CorporateActionIntegrationLedgerRow | None:
    async with factory() as sess:
        return (
            await sess.execute(
                select(CorporateActionIntegrationLedgerRow).where(
                    CorporateActionIntegrationLedgerRow.alpaca_activity_id == alpaca_activity_id
                )
            )
        ).scalar_one_or_none()


async def _count_reconciliation_alerts(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


def _approx_equal(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


# A single check is a tuple ``(condition, detail)``; the first ``condition``
# that evaluates falsy halts the run with the matching ``detail``. Validators
# express their per-action-type matrix row as a flat list of these tuples
# instead of a chain of early-return ``if`` statements — keeps the per-validator
# body linear, and ``_run_checks`` carries the only branch.
_Check = tuple[bool, str]


def _run_checks(label: str, checks: Sequence[_Check]) -> ActionRowResult:
    """Return the first failing check, else a PASS row."""
    for ok, detail in checks:
        if not ok:
            return ActionRowResult(label, ok=False, detail=detail)
    return ActionRowResult(label, ok=True)


@dataclass(frozen=True)
class _EquityView:
    """Narrowed view of a position's equity details, plus a fallback sentinel.

    Lets validators pull (ticker, qty, basis) without re-running the isinstance
    branch at each access site. When the underlying details are not equity,
    the view holds zeroed scalars and an empty ticker so the assertion that
    follows ("post-state details not equity") fires first under the
    short-circuit semantics of :func:`_run_checks`.
    """

    is_equity: bool
    ticker: str
    qty: float
    basis: float


def _as_equity_view(pos: PositionRecord) -> _EquityView:
    details = pos.details
    if isinstance(details, EquityPositionDetails):
        return _EquityView(
            True, details.ticker, details.share_count, details.average_cost_basis_per_share
        )
    return _EquityView(False, "", 0.0, 0.0)


def _equity_matrix_checks(
    *,
    pos: PositionRecord,
    expected_qty: float,
    expected_basis: float,
    expected_ticker: str,
    expected_status: PositionStatus = PositionStatus.OPEN,
) -> tuple[_Check, ...]:
    """Standard six-row check block for a CA mutating an equity position.

    Every quantity-mutating equity CA (SPLIT, REVERSE_SPLIT, STOCK_DIVIDEND)
    asserts the same axes: post-equity details type, quantity, basis, ticker,
    status, and the ``corporate_action_adjustment_needed`` flag. The
    ledger-row check is appended per-validator since the ``alpaca_activity_id``
    differs.
    """
    v = _as_equity_view(pos)
    return (
        (v.is_equity, "post-state details not equity"),
        (_approx_equal(v.qty, expected_qty), f"share_count={v.qty}, expected {expected_qty}"),
        (_approx_equal(v.basis, expected_basis), f"basis={v.basis}, expected {expected_basis}"),
        (v.ticker == expected_ticker, f"ticker={v.ticker!r}, expected {expected_ticker!r}"),
        (pos.status == expected_status, f"status={pos.status!r}, expected {expected_status!r}"),
        (pos.corporate_action_adjustment_needed, "ca_adjustment_needed flag not set"),
    )


def _ledger_check(row: CorporateActionIntegrationLedgerRow | None) -> _Check:
    if row is None:
        return False, "missing ledger row"
    if row.processing_status != CorporateActionLedgerStatus.PROCESSED.value:
        return False, f"ledger processing_status={row.processing_status!r}"
    return True, ""


async def _validate_split(factory: async_sessionmaker[AsyncSession]) -> ActionRowResult:
    pos = await _read_position(factory, _POS_SPLIT)
    ledger = await _read_ledger_row(factory, "ca-split-1")
    return _run_checks(
        "SPLIT",
        (
            *_equity_matrix_checks(
                pos=pos,
                expected_qty=_SPLIT_PRE_QTY * _SPLIT_RATIO,
                expected_basis=_SPLIT_PRE_BASIS / _SPLIT_RATIO,
                expected_ticker=_TICKER_SPLIT,
            ),
            _ledger_check(ledger),
        ),
    )


async def _validate_reverse_split(factory: async_sessionmaker[AsyncSession]) -> ActionRowResult:
    pos = await _read_position(factory, _POS_REV_SPLIT)
    ledger = await _read_ledger_row(factory, "ca-rev-split-1")
    return _run_checks(
        "REVERSE_SPLIT",
        (
            *_equity_matrix_checks(
                pos=pos,
                expected_qty=_REV_SPLIT_PRE_QTY / _REV_SPLIT_RATIO,
                expected_basis=_REV_SPLIT_PRE_BASIS * _REV_SPLIT_RATIO,
                expected_ticker=_TICKER_REV_SPLIT,
            ),
            _ledger_check(ledger),
        ),
    )


async def _validate_stock_dividend(factory: async_sessionmaker[AsyncSession]) -> ActionRowResult:
    pos = await _read_position(factory, _POS_STOCK_DIV)
    ledger = await _read_ledger_row(factory, "ca-stock-div-1")
    factor = 1.0 + _STOCK_DIV_RATE
    return _run_checks(
        "STOCK_DIVIDEND",
        (
            *_equity_matrix_checks(
                pos=pos,
                expected_qty=_STOCK_DIV_PRE_QTY * factor,
                expected_basis=_STOCK_DIV_PRE_BASIS / factor,
                expected_ticker=_TICKER_STOCK_DIV,
            ),
            _ledger_check(ledger),
        ),
    )


async def _validate_cash_dividend_long(
    factory: async_sessionmaker[AsyncSession],
) -> ActionRowResult:
    pos = await _read_position(factory, _POS_CD_LONG)
    ledger = await _read_ledger_row(factory, "ca-cd-long-1")
    return _run_checks(
        "CASH_DIVIDEND_LONG",
        (
            # Cash dividends leave quantity/basis/ticker unchanged.
            *_equity_matrix_checks(
                pos=pos,
                expected_qty=_CD_LONG_PRE_QTY,
                expected_basis=_CD_LONG_PRE_BASIS,
                expected_ticker=_TICKER_CD_LONG,
            ),
            _ledger_check(ledger),
        ),
    )


async def _validate_cash_dividend_short(
    factory: async_sessionmaker[AsyncSession],
) -> ActionRowResult:
    pos = await _read_position(factory, _POS_CD_SHORT)
    ledger = await _read_ledger_row(factory, "ca-cd-short-1")
    return _run_checks(
        "CASH_DIVIDEND_SHORT",
        (
            *_equity_matrix_checks(
                pos=pos,
                expected_qty=_CD_SHORT_PRE_QTY,
                expected_basis=_CD_SHORT_PRE_BASIS,
                expected_ticker=_TICKER_CD_SHORT,
            ),
            (pos.direction == Direction.SHORT, f"direction={pos.direction!r}, expected SHORT"),
            _ledger_check(ledger),
        ),
    )


async def _validate_cash_merger(factory: async_sessionmaker[AsyncSession]) -> ActionRowResult:
    pos = await _read_position(factory, _POS_CASH_MERGER)
    ledger = await _read_ledger_row(factory, "ca-cash-merger-1")
    expected_realized = _CASH_MERGER_PROCEEDS - (_CASH_MERGER_PRE_QTY * _CASH_MERGER_PRE_BASIS)
    realized = pos.realized_pnl_to_date_usd
    v = _as_equity_view(pos)
    return _run_checks(
        "CASH_MERGER",
        (
            (v.is_equity, "post-state details not equity"),
            (_approx_equal(v.qty, 0.0), f"share_count={v.qty}, expected 0"),
            (pos.status == PositionStatus.CLOSED, f"status={pos.status!r}, expected CLOSED"),
            (
                realized is not None and _approx_equal(realized, expected_realized),
                f"realized_pnl={realized}, expected {expected_realized}",
            ),
            _ledger_check(ledger),
        ),
    )


async def _validate_stock_merger(factory: async_sessionmaker[AsyncSession]) -> ActionRowResult:
    pos = await _read_position(factory, _POS_STOCK_MERGER)
    ledger = await _read_ledger_row(factory, "ca-stock-merger-1")
    return _run_checks(
        "STOCK_MERGER",
        (
            *_equity_matrix_checks(
                pos=pos,
                expected_qty=_STOCK_MERGER_NEW_QTY,
                expected_basis=_STOCK_MERGER_NEW_BASIS,
                expected_ticker=_TICKER_STOCK_MERGER_NEW,
            ),
            _ledger_check(ledger),
        ),
    )


async def _read_spin_off_child(
    factory: async_sessionmaker[AsyncSession],
) -> PositionRecord | None:
    async with factory() as sess:
        rows = (await sess.execute(select(PositionRow))).scalars().all()
        children = [r for r in rows if r.parent_position_id == _POS_SPIN_OFF]
    if len(children) != 1:
        return None
    return position_row_to_record(children[0])


async def _validate_spin_off(factory: async_sessionmaker[AsyncSession]) -> ActionRowResult:
    parent = await _read_position(factory, _POS_SPIN_OFF)
    child = await _read_spin_off_child(factory)
    ledger = await _read_ledger_row(factory, "ca-spin-off-1")

    parent_v = _as_equity_view(parent)
    # When the child is missing, fall back to placeholder values so the
    # "child is not None" check fires first and the downstream child-specific
    # checks display sensible sentinels rather than crash.
    if child is None:
        child_v = _EquityView(False, "", 0.0, 0.0)
        child_origin: str | None = None
        child_orphan_ok = False
        child_flag_set = False
    else:
        child_v = _as_equity_view(child)
        child_origin = child.origin
        child_orphan_ok = child.thesis_id is None and child.bracket_id is None
        child_flag_set = child.corporate_action_adjustment_needed
    return _run_checks(
        "SPIN_OFF",
        (
            (parent_v.is_equity, "parent details not equity"),
            (_approx_equal(parent_v.qty, _SPIN_OFF_PARENT_QTY), f"parent qty={parent_v.qty}"),
            (
                _approx_equal(parent_v.basis, _SPIN_OFF_PARENT_POST_BASIS),
                f"parent basis={parent_v.basis}",
            ),
            (parent.corporate_action_adjustment_needed, "parent ca_adjustment_needed flag not set"),
            (child is not None, "expected exactly 1 child position, got none"),
            (
                child_origin == f"spin_off_from_{_POS_SPIN_OFF}",
                f"child origin mismatch (got {child_origin!r})",
            ),
            (child_orphan_ok, "child has thesis/bracket binding (should be orphan)"),
            (child_flag_set, "child ca_adjustment_needed flag not set"),
            (child_v.is_equity, "child details not equity"),
            (
                child_v.ticker == _TICKER_SPIN_OFF_CHILD,
                f"child ticker={child_v.ticker!r}, expected {_TICKER_SPIN_OFF_CHILD!r}",
            ),
            (_approx_equal(child_v.qty, _SPIN_OFF_CHILD_QTY), f"child qty={child_v.qty}"),
            (_approx_equal(child_v.basis, _SPIN_OFF_CHILD_BASIS), f"child basis={child_v.basis}"),
            _ledger_check(ledger),
        ),
    )


@dataclass(frozen=True)
class _OptionsView:
    """Narrowed view of a position's options-details payload (parallels :class:`_EquityView`)."""

    is_options: bool
    underlying: str
    contract_count: float
    premium_per_contract: float


def _as_options_view(pos: PositionRecord) -> _OptionsView:
    details = pos.details
    if isinstance(details, OptionsPositionDetails):
        return _OptionsView(
            True,
            details.underlying_ticker,
            details.contract_count,
            details.premium_paid_per_contract,
        )
    return _OptionsView(False, "", 0.0, 0.0)


async def _validate_symbol_change(factory: async_sessionmaker[AsyncSession]) -> ActionRowResult:
    pos = await _read_position(factory, _POS_SYMBOL_CHANGE)
    ledger = await _read_ledger_row(factory, "ca-symbol-change-1")
    v = _as_options_view(pos)
    return _run_checks(
        "SYMBOL_CHANGE",
        (
            (v.is_options, "post-state details not options"),
            (
                v.underlying == _TICKER_SYMBOL_CHANGE_NEW,
                f"underlying_ticker={v.underlying!r}, expected {_TICKER_SYMBOL_CHANGE_NEW!r}",
            ),
            (
                _approx_equal(v.contract_count, _SYMBOL_CHANGE_PRE_CONTRACTS),
                f"contract_count={v.contract_count}",
            ),
            (
                _approx_equal(v.premium_per_contract, _SYMBOL_CHANGE_PRE_PREMIUM),
                f"premium_paid_per_contract={v.premium_per_contract}",
            ),
            (pos.corporate_action_adjustment_needed, "ca_adjustment_needed flag not set"),
            (pos.status == PositionStatus.OPEN, f"status={pos.status!r}"),
            _ledger_check(ledger),
        ),
    )


_VALIDATORS = (
    (CorporateActionType.SPLIT, _validate_split),
    (CorporateActionType.REVERSE_SPLIT, _validate_reverse_split),
    (CorporateActionType.STOCK_DIVIDEND, _validate_stock_dividend),
    (CorporateActionType.CASH_DIVIDEND_LONG, _validate_cash_dividend_long),
    (CorporateActionType.CASH_DIVIDEND_SHORT, _validate_cash_dividend_short),
    (CorporateActionType.CASH_MERGER, _validate_cash_merger),
    (CorporateActionType.STOCK_MERGER, _validate_stock_merger),
    (CorporateActionType.SPIN_OFF, _validate_spin_off),
    (CorporateActionType.SYMBOL_CHANGE, _validate_symbol_change),
)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def run_verify(db_path: Path) -> VerifyResult:
    """Seed, drain CAs, then assert the per-action-type matrix + reconciliation."""
    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_substrate_and_singletons(factory)
        scenarios = _all_scenarios()
        await _seed_scenarios(factory, scenarios)

        activities = _ca_activities()
        alpaca_positions = _alpaca_positions_aligned()
        alpaca_account = _alpaca_account_aligned()

        async with InvocationContext(
            session_factory=factory, record=_invocation_record_for_phase1()
        ) as handle:
            summary = await process_unprocessed_fills(
                handle,
                ca_activities=activities,
                alpaca_positions=alpaca_positions,
                alpaca_account=alpaca_account,
                market_inputs=_market_inputs(),
                config=_state_persistence_config(),
            )

        # Assert each action type's post-state against the design matrix.
        action_rows: list[ActionRowResult] = []
        for action_type, validator in _VALIDATORS:
            try:
                row = await validator(factory)
            except Exception as exc:
                row = ActionRowResult(action_type.value, ok=False, detail=f"exception: {exc!r}")
            action_rows.append(row)

        alerts_emitted = await _count_reconciliation_alerts(factory)
        # The CASH_DIVIDEND_LONG snapshot is deliberately off by one share, so we
        # expect exactly one alert.
        expected_alerts = 1
        reconciliation_ok = (
            alerts_emitted == expected_alerts and summary.reconciliation_alerts == expected_alerts
        )

        return VerifyResult(
            action_rows=tuple(action_rows),
            reconciliation_alerts_emitted=alerts_emitted,
            reconciliation_alerts_expected=expected_alerts,
            reconciliation_summary_ok=reconciliation_ok,
        )
    finally:
        await engine.dispose()


def _invocation_record_for_phase1() -> InvocationRecord:
    """Distinct invocation row for the Phase 1 run.

    The seed step inserts the bootstrap ``_INVOCATION_ID`` row; Phase 1 opens
    its own ``InvocationContext`` which must insert a fresh row, so we name it
    with a suffix to avoid the PK collision.
    """
    base = _invocation_record()
    return base.model_copy(update={"invocation_id": f"{_INVOCATION_ID}-phase1"})


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def _render_text(result: VerifyResult) -> str:
    lines = ["=" * 70, "AlphaMind Corporate-Actions Verification", "=" * 70]
    for r in result.action_rows:
        status = "PASS" if r.ok else "FAIL"
        lines.append(f"  {r.label:<25} {status}")
        if r.detail:
            lines.append(f"      {r.detail}")
    lines.append("-" * 70)
    recon_status = "PASS" if result.reconciliation_summary_ok else "FAIL"
    lines.append(
        f"  Reconciliation             {recon_status} "
        f"(alerts emitted={result.reconciliation_alerts_emitted}, "
        f"expected={result.reconciliation_alerts_expected})"
    )
    lines.append("=" * 70)
    if result.ok:
        lines.append("ALL ACTION TYPES PASS")
    else:
        failed = [r.label for r in result.action_rows if not r.ok]
        if not result.reconciliation_summary_ok:
            failed.append("Reconciliation")
        lines.append(f"FAIL: {len(failed)} check(s) failed: {', '.join(failed)}")
    lines.append("=" * 70)
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the corporate-actions pipeline end-to-end against a freshly-"
            "migrated SQLite DB across the per-action-type matrix from "
            "docs/design/05-execution-layer/corporate-actions.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=(
            "Path to a freshly-migrated SQLite DB. When omitted, a temporary DB "
            "is created and migrated to head for this run."
        ),
    )
    return parser


def _resolve_db_path(arg_db_path: Path | None) -> tuple[Path, bool]:
    """Return ``(db_path, owns_path)`` — when ``owns_path`` is True, the script
    created the file under tempdir and the caller is responsible for keeping
    it for the run's duration (no cleanup; tempdir cleans up).
    """
    if arg_db_path is not None:
        return arg_db_path, False
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="verify-corporate-actions-"))
    db_path = tmp / "alphamind.db"
    _migrate_db_to_head(db_path)
    return db_path, True


def _migrate_db_to_head(db_path: Path) -> None:
    """Apply the state-persistence schema via ``Base.metadata.create_all``."""
    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.state.tables  # noqa: F401
    from alphamind.persistence.models import Base

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on all-pass, 1 on any failure."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path, _owns = _resolve_db_path(args.db_path)

    result = asyncio.run(run_verify(db_path))
    print(_render_text(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
