"""Reg T margin attribution end-to-end verification (ALP-430, story 07).

Operator entry point (via thin shim at
``scripts/verify_regt_margin_attribution.py``). Exercises the per-fill Reg T
margin attribution work tree end-to-end against a freshly-migrated SQLite DB:

* Seeds a representative four-position portfolio (long NVDA equity, short AMD
  equity, long SPY call, NVDA bull-call-spread strategy) so the pre-fill Reg T
  and PM-equivalent margins are non-trivial.
* Seeds two unprocessed fills targeting new orders: a BUY entry fill on a
  PENDING NVDA long-equity position, and a SELL partial-exit fill on a separate
  OPEN AMD long-equity position. (Phase 1 does not support SHORT entry fills
  per the state-persistence runbook, so the AMD-side fill is an exit on a
  long sub-position rather than a SELL_TO_OPEN against the seeded short.)
* Constructs a ``MarketInputs`` covering every open-position underlying plus a
  ``FixtureIvProvider`` with IV quotes for every option leg.
* Drives ``process_unprocessed_fills`` inside an ``InvocationContext`` so the
  Phase 1 wedge populates each fill's ``regt_attribution_json`` column.
* Rehydrates the per-fill ``RegTMarginAttribution`` records and prints all
  eight fields per fill.
* Invokes the snapshot assembler so its Step 11 ``RegTExcessAggregates``
  enrichment lands on ``CashLedger`` and prints the three trailing-window
  fields.
* Asserts:
  - All 8 attribution fields are finite per fill.
  - For each fill, ``regt_excess_over_pm == regt_marginal_consumption -
    pm_marginal_consumption`` within ``1e-6``.
  - The trailing-30d aggregate equals the sum of the two fills'
    ``regt_excess_over_pm`` within ``1e-6``.

Exits 0 on PASS, 1 on a structured FAIL block naming the failing assertion.

No SDK invocation; no live broker contact. Sub-second runtime against a fresh
on-disk DB.

Usage::

    uv run python scripts/verify_regt_margin_attribution.py \\
        [--db-path PATH] [--invocation-id ID] [--verbose]

See ``scripts/RUNBOOK_regt_margin_attribution.md`` for the operator runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price
from alphamind._kernel.regime import RiskZone
from alphamind.execution.regt_margin_attribution import (
    load_regt_margin_attribution_config,
)
from alphamind.execution.write_paths.fill_persistence import (
    append_fill_record,
)
from alphamind.execution.write_paths.phase1 import (
    process_unprocessed_fills,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
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
)
from alphamind.risk_guardrails.guardrail_evaluation.iv_sourcing import (
    IvQuote,
    IvSurfaceEntry,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.context import (
    InvocationContext,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
    RegTMarginAttribution,
)
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

__all__ = [
    "EXIT_FAIL",
    "EXIT_PASS",
    "AttributionRow",
    "VerifyResult",
    "main",
    "rehydrate_attributions",
    "run_verify",
    "seed_portfolio_and_fills",
]


EXIT_PASS = 0
EXIT_FAIL = 1

# Tolerance for float-equality assertions. The headline algebra
# (regt_excess_over_pm == regt_marginal - pm_marginal) is exact in IEEE 754
# subtraction so ``1e-6`` is conservative; the trailing-30d aggregate equality
# adds two same-magnitude fills back together so the same epsilon holds.
_TOL = 1e-6


# ---------------------------------------------------------------------------
# Fixture constants — kept narrow + stable so output is deterministic.
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_PROCESS_ID = "verify-regt-proc"
_DEFAULT_INVOCATION_ID = "verify-regt-001"

# Seeded portfolio (all OPEN) — supplies non-trivial pre-fill margin state.
_NVDA = "NVDA"
_AMD = "AMD"
_SPY = "SPY"
_SEED_NVDA_LONG_POS = "verify-regt-pos-nvda-long"
_SEED_AMD_SHORT_POS = "verify-regt-pos-amd-short"
_SEED_SPY_CALL_POS = "verify-regt-pos-spy-call"
_SEED_NVDA_SPREAD_POS = "verify-regt-pos-nvda-spread"

# Fill scaffolding (one new PENDING NVDA buy, one new OPEN AMD long with a SELL exit).
_BUY_FILL_ID = "verify-regt-fill-nvda-buy"
_BUY_ORDER_ID = "verify-regt-ord-nvda-buy"
_BUY_BRACKET_ID = "verify-regt-brk-nvda-buy"
_BUY_POSITION_ID = "verify-regt-pos-nvda-pending"
_BUY_THESIS_ID = "verify-regt-thesis-nvda-buy"

_SELL_FILL_ID = "verify-regt-fill-amd-sell"
_SELL_ORDER_ID = "verify-regt-ord-amd-sell"
_SELL_BRACKET_ID = "verify-regt-brk-amd-sell"
_SELL_POSITION_ID = "verify-regt-pos-amd-long-exit"
_SELL_THESIS_ID = "verify-regt-thesis-amd-sell"
# Historical (filled-and-done) AMD entry order that opened the AMD long
# sub-position; required by the bracket's ``entry_order_id`` reference but
# not exercised by Phase 1 — the active fill is the exit order above.
_SELL_HISTORICAL_ENTRY_ORDER_ID = "verify-regt-ord-amd-historical-entry"

# Spread option-leg shared expiration.
_SPREAD_EXPIRATION = date(2026, 8, 21)
_SPY_CALL_EXPIRATION = date(2026, 7, 17)

# Spot prices used both for ``MarketInputs`` and the assembler's price provider.
_PRICES_USD: dict[str, float] = {
    _NVDA: 925.0,
    _AMD: 145.0,
    _SPY: 540.0,
}

_FIXTURE_GREEKS = OptionGreeks(delta=0.45, gamma=0.04, theta=-0.02, vega=0.12)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionRow:
    """One processed fill's rehydrated attribution + the originating fill id."""

    fill_id: str
    attribution: RegTMarginAttribution


@dataclass(frozen=True)
class VerifyResult:
    """End-to-end verification outcome bundled for rendering.

    ``trailing_30d_usd`` / ``trailing_90d_usd`` / ``lifetime_usd`` are the
    snapshot assembler's Step 11 enriched ``CashLedger`` fields; ``failures``
    is empty on a clean PASS.
    """

    attributions: tuple[AttributionRow, ...]
    trailing_30d_usd: float
    trailing_90d_usd: float
    lifetime_usd: float
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/verify-regt",
            "invocation_provenance_root": "/tmp/verify-regt",
        }
    )


def _portfolio_state_config() -> PortfolioStateConfig:
    return PortfolioStateConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 5,
            "thesis_resolutions_lookback_trading_days": 10,
            "thesis_quality_aggregates_trailing_windows_days": (5, 20),
            "snapshot_freshness_max_phase1_to_snapshot_seconds": 300.0,
            "snapshot_freshness_max_price_age_seconds": 60.0,
        }
    )


def _process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=23456,
        hostname="verify-regt",
        git_sha="b" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/verify-regt/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="verify-regt",
    )


def _invocation_record(invocation_id: str, *, start_at: datetime | None = None) -> InvocationRecord:
    started = start_at if start_at is not None else _NOW
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=started.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="verify_regt_margin_attribution.py",
        trigger_reason="operator-triggered verification",
        git_sha_at_invocation="b" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/verify-regt/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/verify-regt/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _open_async_factory(
    db_path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(engine)
    return engine, factory


def _execution_history_fill(
    *,
    fill_quantity: float,
    fill_price: float,
    fill_timestamp: datetime,
) -> PositionFill:
    return PositionFill(
        fill_timestamp=fill_timestamp,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        slippage=0.0,
        fees=0.0,
    )


# ---------------------------------------------------------------------------
# Seeded portfolio (four OPEN positions) — the pre-fill state.
# ---------------------------------------------------------------------------


def _seed_long_nvda_equity() -> PositionRecord:
    """OPEN long NVDA equity; contributes to the NVDA class-group margin."""
    return PositionRecord(
        position_id=PositionId(_SEED_NVDA_LONG_POS),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=48),
        details=EquityPositionDetails(
            ticker=Symbol(_NVDA),
            share_count=50.0,
            average_cost_basis_per_share=900.0,
        ),
        execution_history=(
            _execution_history_fill(
                fill_quantity=50.0,
                fill_price=900.0,
                fill_timestamp=_NOW - timedelta(hours=48),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _seed_short_amd_equity() -> PositionRecord:
    """OPEN short AMD equity; contributes to the AMD class-group margin."""
    return PositionRecord(
        position_id=PositionId(_SEED_AMD_SHORT_POS),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=_NOW - timedelta(hours=36),
        details=EquityPositionDetails(
            ticker=Symbol(_AMD),
            share_count=100.0,
            average_cost_basis_per_share=150.0,
            borrow_rate_pct=0.04,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=15_000.0,
        ),
        execution_history=(
            _execution_history_fill(
                fill_quantity=100.0,
                fill_price=150.0,
                fill_timestamp=_NOW - timedelta(hours=36),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _seed_long_spy_call() -> PositionRecord:
    """OPEN long SPY call; contributes to the SPY class-group margin."""
    details = OptionsPositionDetails(
        underlying_ticker=Symbol(_SPY),
        strike_price=545.0,
        expiration_date=_SPY_CALL_EXPIRATION,
        contract_type=OptionContractType.CALL,
        contract_count=5.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=8.0,
        greeks=_FIXTURE_GREEKS,
    )
    return PositionRecord(
        position_id=PositionId(_SEED_SPY_CALL_POS),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=24),
        details=details,
        execution_history=(
            _execution_history_fill(
                fill_quantity=5.0,
                fill_price=8.0,
                fill_timestamp=_NOW - timedelta(hours=24),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _seed_nvda_bull_call_spread() -> PositionRecord:
    """OPEN NVDA bull call spread (long 900, short 950); contributes to NVDA class-group."""
    long_leg_details = OptionsPositionDetails(
        underlying_ticker=Symbol(_NVDA),
        strike_price=900.0,
        expiration_date=_SPREAD_EXPIRATION,
        contract_type=OptionContractType.CALL,
        contract_count=2.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=45.0,
        greeks=_FIXTURE_GREEKS,
    )
    short_leg_details = OptionsPositionDetails(
        underlying_ticker=Symbol(_NVDA),
        strike_price=950.0,
        expiration_date=_SPREAD_EXPIRATION,
        contract_type=OptionContractType.CALL,
        contract_count=2.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=22.0,
        greeks=_FIXTURE_GREEKS,
    )
    details = StrategyPositionDetails(
        strategy_type_label="bull_call_spread",
        legs=(
            StrategyLeg(leg_id="long_call", direction=Direction.LONG, options=long_leg_details),
            StrategyLeg(leg_id="short_call", direction=Direction.SHORT, options=short_leg_details),
        ),
        net_premium_usd=-4_600.0,
        max_profit_usd=5_400.0,
        max_loss_usd=-4_600.0,
        breakeven_levels=(923.0,),
        strategy_greeks=_FIXTURE_GREEKS,
    )
    return PositionRecord(
        position_id=PositionId(_SEED_NVDA_SPREAD_POS),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=18),
        details=details,
        execution_history=(
            _execution_history_fill(
                fill_quantity=2.0,
                fill_price=23.0,
                fill_timestamp=_NOW - timedelta(hours=18),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _seeded_portfolio() -> tuple[PositionRecord, ...]:
    return (
        _seed_long_nvda_equity(),
        _seed_short_amd_equity(),
        _seed_long_spy_call(),
        _seed_nvda_bull_call_spread(),
    )


# ---------------------------------------------------------------------------
# Fill scaffolding — orders, positions, brackets, theses, and the two fills.
# ---------------------------------------------------------------------------


def _buy_entry_position_pending() -> PositionRecord:
    """PENDING NVDA long equity awaiting the buy entry fill."""
    return PositionRecord(
        position_id=PositionId(_BUY_POSITION_ID),
        thesis_id=ThesisId(_BUY_THESIS_ID),
        bracket_id=BracketId(_BUY_BRACKET_ID),
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=EquityPositionDetails(
            ticker=Symbol(_NVDA),
            share_count=0.0,
            average_cost_basis_per_share=0.0,
        ),
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _buy_entry_order() -> OrderRecord:
    return OrderRecord.model_validate(
        {
            "order_id": _BUY_ORDER_ID,
            "position_id": None,
            "bracket_id": _BUY_BRACKET_ID,
            "role": OrderRole.ENTRY,
            "instrument_spec": EquityInstrumentSpec(ticker=Symbol(_NVDA)),
            "direction": OrderDirection.BUY,
            "order_type": OrderType.MARKET,
            "order_class": OrderClass.SIMPLE,
            "price_parameters": PriceParameters(),
            "quantity": 10.0,
            "duration": OrderDuration.DAY,
            "status": OrderStatus.PENDING,
            "alpaca_order_id": f"alp-{_BUY_ORDER_ID}",
            "alpaca_order_id_chain": (f"alp-{_BUY_ORDER_ID}",),
            "submission_timestamp": _NOW - timedelta(minutes=20),
            "last_update_timestamp": _NOW - timedelta(minutes=20),
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": 10.0,
            "modification_count": 0,
            "originating_thesis_id": _BUY_THESIS_ID,
            "originating_pm_command_id": None,
            "age_hours": 0.33,
        }
    )


def _buy_entry_protective_stop_order() -> OrderRecord:
    stop_id = f"{_BUY_BRACKET_ID}-ord-stop"
    return OrderRecord.model_validate(
        {
            "order_id": stop_id,
            "position_id": None,
            "bracket_id": _BUY_BRACKET_ID,
            "role": OrderRole.PRICE_STOP,
            "instrument_spec": EquityInstrumentSpec(ticker=Symbol(_NVDA)),
            "direction": OrderDirection.SELL,
            "order_type": OrderType.STOP,
            "order_class": OrderClass.SIMPLE,
            "price_parameters": PriceParameters(stop_trigger_price=850.0),
            "quantity": 10.0,
            "duration": OrderDuration.GTC,
            "status": OrderStatus.PENDING,
            "alpaca_order_id": f"alp-{stop_id}",
            "alpaca_order_id_chain": (f"alp-{stop_id}",),
            "submission_timestamp": _NOW - timedelta(minutes=20),
            "last_update_timestamp": _NOW - timedelta(minutes=20),
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": 10.0,
            "modification_count": 0,
            "originating_thesis_id": _BUY_THESIS_ID,
            "originating_pm_command_id": None,
            "age_hours": 0.33,
        }
    )


def _buy_entry_bracket() -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{_BUY_BRACKET_ID}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{_BUY_BRACKET_ID}-ord-stop"),
        trigger=PriceTrigger(underlying_ticker=Symbol(_NVDA), threshold_usd=850.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(_BUY_BRACKET_ID),
        position_id=PositionId(_BUY_POSITION_ID),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId(_BUY_ORDER_ID),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _buy_entry_thesis() -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{_BUY_THESIS_ID}-{ct.value.lower()}",
            thesis_id=ThesisId(_BUY_THESIS_ID),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=_NVDA,
            narrative=f"{ct.value} narrative for NVDA buy",
            key_assumptions=(KeyAssumption(text="Capex tailwind persists", outcome=None),),
            generation_timestamp=_NOW - timedelta(hours=2),
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    gen = _NOW - timedelta(hours=2)
    return ThesisRecord(
        thesis_id=ThesisId(_BUY_THESIS_ID),
        position_id=PositionId(_BUY_POSITION_ID),
        summary="NVDA momentum",
        key_catalyst="GTC roadmap update",
        position_size_rationale="Sized at 1% conviction-3",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=gen,
        time_expectation_hours=24.0,
        age_hours=2.0,
        expected_resolution_at=gen + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _sell_exit_position_open() -> PositionRecord:
    """OPEN long AMD equity sized to absorb the partial-exit sell fill.

    Mirrors the story's intent (a sell fill on AMD) within Phase 1's supported
    paths: Phase 1 raises ``NotImplementedError`` on SHORT-side entry fills,
    so the sell is modelled as a partial exit on a separate long sub-position
    rather than a ``SELL_TO_OPEN`` against the seeded AMD short.
    """
    return PositionRecord(
        position_id=PositionId(_SELL_POSITION_ID),
        thesis_id=ThesisId(_SELL_THESIS_ID),
        bracket_id=BracketId(_SELL_BRACKET_ID),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=8),
        details=EquityPositionDetails(
            ticker=Symbol(_AMD),
            share_count=20.0,
            average_cost_basis_per_share=140.0,
        ),
        execution_history=(
            _execution_history_fill(
                fill_quantity=20.0,
                fill_price=140.0,
                fill_timestamp=_NOW - timedelta(hours=8),
            ),
        ),
        realized_pnl_to_date_usd=0.0,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _sell_historical_entry_order() -> OrderRecord:
    """Filled-and-done AMD entry order kept so the bracket's ``entry_order_id``
    FK resolves. Not exercised by Phase 1 — the sell exit fill targets a
    separate order."""
    return OrderRecord.model_validate(
        {
            "order_id": _SELL_HISTORICAL_ENTRY_ORDER_ID,
            "position_id": _SELL_POSITION_ID,
            "bracket_id": _SELL_BRACKET_ID,
            "role": OrderRole.ENTRY,
            "instrument_spec": EquityInstrumentSpec(ticker=Symbol(_AMD)),
            "direction": OrderDirection.BUY,
            "order_type": OrderType.MARKET,
            "order_class": OrderClass.SIMPLE,
            "price_parameters": PriceParameters(),
            "quantity": 20.0,
            "duration": OrderDuration.DAY,
            "status": OrderStatus.FILLED,
            "alpaca_order_id": f"alp-{_SELL_HISTORICAL_ENTRY_ORDER_ID}",
            "alpaca_order_id_chain": (f"alp-{_SELL_HISTORICAL_ENTRY_ORDER_ID}",),
            "submission_timestamp": _NOW - timedelta(hours=8),
            "last_update_timestamp": _NOW - timedelta(hours=8),
            "filled_quantity": 20.0,
            "avg_fill_price": 140.0,
            "remaining_quantity": 0.0,
            "modification_count": 0,
            "originating_thesis_id": _SELL_THESIS_ID,
            "originating_pm_command_id": None,
            "age_hours": 8.0,
        }
    )


def _sell_exit_order() -> OrderRecord:
    return OrderRecord.model_validate(
        {
            "order_id": _SELL_ORDER_ID,
            "position_id": _SELL_POSITION_ID,
            "bracket_id": _SELL_BRACKET_ID,
            "role": OrderRole.TAKE_PROFIT,
            "instrument_spec": EquityInstrumentSpec(ticker=Symbol(_AMD)),
            "direction": OrderDirection.SELL,
            "order_type": OrderType.LIMIT,
            "order_class": OrderClass.SIMPLE,
            "price_parameters": PriceParameters(limit_price=145.0),
            "quantity": 10.0,
            "duration": OrderDuration.GTC,
            "status": OrderStatus.PENDING,
            "alpaca_order_id": f"alp-{_SELL_ORDER_ID}",
            "alpaca_order_id_chain": (f"alp-{_SELL_ORDER_ID}",),
            "submission_timestamp": _NOW - timedelta(minutes=10),
            "last_update_timestamp": _NOW - timedelta(minutes=10),
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": 10.0,
            "modification_count": 0,
            "originating_thesis_id": _SELL_THESIS_ID,
            "originating_pm_command_id": None,
            "age_hours": 0.17,
        }
    )


def _sell_exit_bracket() -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{_SELL_BRACKET_ID}-leg-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId(_SELL_ORDER_ID),
        trigger=PriceTrigger(underlying_ticker=Symbol(_AMD), threshold_usd=145.0, direction="GTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(_SELL_BRACKET_ID),
        position_id=PositionId(_SELL_POSITION_ID),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId(_SELL_HISTORICAL_ENTRY_ORDER_ID),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _sell_exit_thesis() -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{_SELL_THESIS_ID}-{ct.value.lower()}",
            thesis_id=ThesisId(_SELL_THESIS_ID),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=_AMD,
            narrative=f"{ct.value} narrative for AMD exit",
            key_assumptions=(KeyAssumption(text="Take partial profit", outcome=None),),
            generation_timestamp=_NOW - timedelta(hours=6),
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    gen = _NOW - timedelta(hours=6)
    return ThesisRecord(
        thesis_id=ThesisId(_SELL_THESIS_ID),
        position_id=PositionId(_SELL_POSITION_ID),
        summary="AMD partial trim",
        key_catalyst="Resistance at $145",
        position_size_rationale="Partial exit on technical level",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=gen,
        time_expectation_hours=12.0,
        age_hours=6.0,
        expected_resolution_at=gen + timedelta(hours=12),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _buy_fill_record() -> FillRecord:
    # ALP-462 — wrap fixture floats at the FillRecord boundary.
    return FillRecord(
        fill_id=_BUY_FILL_ID,
        order_id=_BUY_ORDER_ID,
        fill_timestamp=_NOW - timedelta(minutes=12),
        fill_price=price("920"),
        fill_quantity=10.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=money("0"),
        fees_usd=money("0"),
        execution_venue="NASDAQ",
        gateway_reference=f"alp-{_BUY_FILL_ID}",
        persistence_timestamp=_NOW - timedelta(minutes=12) + timedelta(seconds=1),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


def _sell_fill_record() -> FillRecord:
    return FillRecord(
        fill_id=_SELL_FILL_ID,
        order_id=_SELL_ORDER_ID,
        fill_timestamp=_NOW - timedelta(minutes=6),
        fill_price=price("146"),
        fill_quantity=10.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=money("0"),
        fees_usd=money("0"),
        execution_venue="NASDAQ",
        gateway_reference=f"alp-{_SELL_FILL_ID}",
        persistence_timestamp=_NOW - timedelta(minutes=6) + timedelta(seconds=1),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


# ---------------------------------------------------------------------------
# Cash + drawdown singletons
# ---------------------------------------------------------------------------


def _cash_ledger() -> CashLedger:
    return CashLedger.model_validate(
        {
            "current_cash_usd": 250_000.0,
            "settled_cash_usd": 250_000.0,
            "reserved_capital_usd": 9_500.0,  # entry-order reservation for the buy
            "available_buying_power_usd": 240_500.0,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )


def _drawdown_state() -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": 0.0,
            "equity_high_water_mark_usd": 250_000.0,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": 0.0,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


# ---------------------------------------------------------------------------
# Market inputs
# ---------------------------------------------------------------------------


def _build_iv_provider() -> FixtureIvProvider:
    """IV surface covering every option leg in the seeded portfolio + class-group expansions.

    Provides IV quotes at the spread's two strikes (900, 950), the SPY call's
    strike (545), and the stress shock-grid neighbourhood for each underlying so
    the PM-equivalent stress revaluation never falls back to realized-vol or
    raises ``IvLookupError``. Realized-vol fallback entries are still supplied
    as a belt-and-braces safety net.
    """
    nvda_strikes = (850.0, 900.0, 925.0, 950.0, 1000.0)
    spy_strikes = (510.0, 540.0, 545.0, 570.0, 600.0)
    nvda_quotes = tuple(
        IvQuote(
            strike=strike,
            expiration=expiration,
            contract_type=ct,
            implied_volatility=0.45,
        )
        for strike in nvda_strikes
        for expiration in (_SPREAD_EXPIRATION,)
        for ct in (ContractType.CALL, ContractType.PUT)
    )
    spy_quotes = tuple(
        IvQuote(
            strike=strike,
            expiration=_SPY_CALL_EXPIRATION,
            contract_type=ct,
            implied_volatility=0.18,
        )
        for strike in spy_strikes
        for ct in (ContractType.CALL, ContractType.PUT)
    )
    return FixtureIvProvider(
        surface={
            _NVDA: IvSurfaceEntry(underlying=_NVDA, quotes=nvda_quotes),
            _SPY: IvSurfaceEntry(underlying=_SPY, quotes=spy_quotes),
        },
        realized_vol={},
    )


def _market_inputs() -> MarketInputs:
    """``MarketInputs`` carrying every open-position underlying + the IV surface."""
    return MarketInputs(
        underlying_prices=dict(_PRICES_USD),
        risk_free_rate=0.0425,
        iv_provider=_build_iv_provider(),
        as_of=_NOW,
    )


# ---------------------------------------------------------------------------
# DB seeding
# ---------------------------------------------------------------------------


async def _seed_process_lifetime(factory: async_sessionmaker[AsyncSession]) -> None:
    """Persist the parent process_lifetime row; idempotent."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from alphamind.state.tables.process_lifetimes import (
        ProcessLifetimeRow,
    )

    row = process_lifetime_record_to_row(_process_lifetime_record())
    values = {col.name: getattr(row, col.name) for col in ProcessLifetimeRow.__table__.columns}
    stmt = sqlite_insert(ProcessLifetimeRow).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["process_lifetime_id"])
    async with factory() as sess:
        await sess.execute(stmt)
        await sess.commit()


async def _seed_singletons(factory: async_sessionmaker[AsyncSession]) -> None:
    """Persist the cash_ledger + drawdown_state singletons."""
    async with factory() as sess:
        sess.add(cash_ledger_record_to_row(_cash_ledger(), last_updated_at=_NOW))
        sess.add(drawdown_state_record_to_row(_drawdown_state(), last_updated_at=_NOW))
        await sess.commit()


async def _seed_portfolio_rows(factory: async_sessionmaker[AsyncSession]) -> None:
    """Persist the four OPEN positions of the seeded portfolio."""
    async with factory() as sess:
        for position in _seeded_portfolio():
            sess.add(position_record_to_row(position))
        await sess.commit()


async def _seed_fill_scaffolding(factory: async_sessionmaker[AsyncSession]) -> None:
    """Persist the orders, positions, brackets, and theses the two fills attach to.

    Two parallel scaffolds: (a) PENDING NVDA position + entry order + protective
    stop + bracket + thesis for the buy fill; (b) OPEN AMD position + sell order
    + bracket + thesis for the sell partial-exit fill.
    """
    buy_bracket_parent, buy_leg_rows = bracket_record_to_rows(_buy_entry_bracket())
    sell_bracket_parent, sell_leg_rows = bracket_record_to_rows(_sell_exit_bracket())
    buy_thesis_parent, buy_component_rows = thesis_record_to_rows(_buy_entry_thesis())
    sell_thesis_parent, sell_component_rows = thesis_record_to_rows(_sell_exit_thesis())

    async with factory() as sess:
        sess.add(order_record_to_row(_buy_entry_order()))
        sess.add(order_record_to_row(_buy_entry_protective_stop_order()))
        sess.add(order_record_to_row(_sell_historical_entry_order()))
        sess.add(order_record_to_row(_sell_exit_order()))
        sess.add(position_record_to_row(_buy_entry_position_pending()))
        sess.add(position_record_to_row(_sell_exit_position_open()))
        sess.add(buy_bracket_parent)
        sess.add(sell_bracket_parent)
        sess.add(buy_thesis_parent)
        sess.add(sell_thesis_parent)
        await sess.flush()
        for r in (*buy_leg_rows, *sell_leg_rows, *buy_component_rows, *sell_component_rows):
            sess.add(r)
        await sess.commit()


async def _seed_unprocessed_fills(factory: async_sessionmaker[AsyncSession]) -> None:
    """Append the two unprocessed fills via the canonical write path."""
    async with factory() as sess:
        await append_fill_record(sess, _buy_fill_record())
        await append_fill_record(sess, _sell_fill_record())
        await sess.commit()


async def seed_portfolio_and_fills(factory: async_sessionmaker[AsyncSession]) -> None:
    """End-to-end seeding: process lifetime + singletons + portfolio + scaffolding + fills."""
    await _seed_process_lifetime(factory)
    await _seed_singletons(factory)
    await _seed_portfolio_rows(factory)
    await _seed_fill_scaffolding(factory)
    await _seed_unprocessed_fills(factory)


# ---------------------------------------------------------------------------
# Phase 1 drive + rehydration
# ---------------------------------------------------------------------------


async def _run_phase1_drain(
    factory: async_sessionmaker[AsyncSession], *, invocation_id: str
) -> None:
    """Drive ``process_unprocessed_fills`` inside an ``InvocationContext``."""
    # Use a wall-time anchor so the assembler's freshness check (phase1 stamp
    # at or before snapshot.now) holds whichever direction wall time runs.
    record = _invocation_record(invocation_id, start_at=datetime.now(UTC) + timedelta(seconds=5))
    async with InvocationContext(session_factory=factory, record=record) as handle:
        await process_unprocessed_fills(
            handle,
            market_inputs=_market_inputs(),
            config=_state_persistence_config(),
        )


async def rehydrate_attributions(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[AttributionRow, ...]:
    """Read processed fill rows and rehydrate their attribution payloads.

    Returns the attribution rows in fill_timestamp order so output is
    deterministic (buy NVDA before sell AMD).
    """
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(FillRecordRow)
                    .where(
                        FillRecordRow.processing_status == FillProcessingStatus.PROCESSED.value,
                        FillRecordRow.regt_attribution_json.is_not(None),
                    )
                    .order_by(FillRecordRow.fill_timestamp.asc(), FillRecordRow.fill_id.asc())
                )
            )
            .scalars()
            .all()
        )
    out: list[AttributionRow] = []
    for row in rows:
        if row.regt_attribution_json is None:
            continue
        attribution = RegTMarginAttribution.model_validate_json(row.regt_attribution_json)
        out.append(AttributionRow(fill_id=row.fill_id, attribution=attribution))
    return tuple(out)


# ---------------------------------------------------------------------------
# Assembler invocation — surfaces trailing-window aggregates on CashLedger.
# ---------------------------------------------------------------------------


async def _run_assembler_for_aggregates(
    factory: async_sessionmaker[AsyncSession], *, invocation_id: str
) -> CashLedger:
    """Invoke the snapshot assembler and return the enriched ``CashLedger``.

    The assembler's Step 11 reads :class:`RegTExcessAggregates` from the
    repository and copies the three fields onto ``CashLedger`` — the same
    delivery surface the command-center consumes.
    """
    from alphamind._kernel.regime import (
        RegimeLabel,
        RegimeTransitionState,
    )
    from alphamind.config.guardrails_helpers import (
        load_cumulative_drawdown_progressive_tiers,
    )
    from alphamind.config.models.regimes import Regime
    from alphamind.execution.guardrail_enforcement import (
        compose_phase_1_enforcement,
        make_active_risk_parameters_provider,
    )
    from alphamind.portfolio_state.aggregates.risk_parameters import (
        ActiveRiskParameterEntry,
        ActiveRiskParameterSet,
    )
    from alphamind.risk_guardrails.regime_adaptation import (
        RegimeAdaptationOutput,
        RegimeAdaptationState,
    )
    from alphamind.state.repository import (
        build_sql_portfolio_state_repository,
    )

    parameters = ActiveRiskParameterSet.model_validate(
        {
            "regime_label": RegimeLabel.NORMAL,
            "transition_state": RegimeTransitionState.STABLE,
            "transition_invocations_remaining": 0,
            "parameter_change_flag": False,
            "entries": (
                ActiveRiskParameterEntry.model_validate(
                    {
                        "rule_id": "max_position_size_usd",
                        "rule_label": "Max position size (USD)",
                        "value": 25_000.0,
                        "unit": "USD",
                        "regime_multiplier_applied": 1.0,
                        "base_value": 25_000.0,
                    }
                ),
            ),
            "active_overlays": (),
        }
    )
    regime_state = RegimeAdaptationState(
        as_of=_NOW.isoformat().replace("+00:00", "Z"),
        invocation_id=invocation_id,
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="normal",
        distillation_vix_level=18.0,
        regime_skip_emergency=False,
    )
    regime_output = RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=parameters,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=regime_state,
        audit_log_entries=(),
    )
    phase1_result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=_drawdown_state(),
        progressive_tiers=load_cumulative_drawdown_progressive_tiers(),
    )
    provider = make_active_risk_parameters_provider(phase1_result)

    def _prior_provider(_path: str) -> ActiveRiskParameterSet:
        return parameters

    repo = build_sql_portfolio_state_repository(
        session_factory=factory,
        invocation_id=invocation_id,
        active_risk_parameters_provider=provider,
        prior_active_risk_parameters_provider=_prior_provider,
        config=_state_persistence_config(),
    )

    now = datetime.now(UTC)
    price_provider = StubCurrentPriceProvider(
        {
            ticker: PriceQuote(
                ticker=ticker,
                price_usd=price,
                as_of_timestamp=now,
                source=PriceSource.INTRADAY_QUOTE,
                is_stale=False,
            )
            for ticker, price in _PRICES_USD.items()
        },
        now=now,
    )

    def _sector_resolver(_pos: PositionRecord) -> str | None:
        return "tech"

    assembled = assemble_snapshot(
        repository=repo,
        price_provider=price_provider,
        sector_resolver=_sector_resolver,
        config=_portfolio_state_config(),
        now=now,
    )
    return assembled.snapshot.cash_ledger


# ---------------------------------------------------------------------------
# Assertions — the runbook-named exit conditions.
# ---------------------------------------------------------------------------


def _collect_assertion_failures(
    attributions: Sequence[AttributionRow],
    *,
    trailing_30d_usd: float,
    trailing_90d_usd: float | None = None,
    lifetime_usd: float | None = None,
) -> tuple[str, ...]:
    """Return the structured FAIL messages; empty tuple on a clean PASS.

    Each message names the failing assertion in the runbook's failure-mode
    triage table so the operator's first move is a triage-table lookup.

    Both seeded fills fall within 30 days of ``now`` by construction, so all
    three windows (``trailing_30d_usd``, ``trailing_90d_usd``, ``lifetime_usd``)
    must equal the per-fill ``regt_excess_over_pm`` sum within ``_TOL``. The
    longer-window arguments default to ``None`` so unit tests that pre-date
    the cross-window guard keep compiling; production calls pass all three
    (a Step 11 wiring regression that drops one window surfaces here).
    """
    failures: list[str] = []

    if len(attributions) != 2:
        failures.append(
            f"expected 2 processed fills with regt_attribution_json, got {len(attributions)}"
        )
        return tuple(failures)

    for row in attributions:
        if not _all_fields_finite(row.attribution):
            failures.append(
                f"attribution fields not all finite for fill_id={row.fill_id!r}; "
                f"see runbook § Failure-mode triage row 'non-finite attribution field'"
            )

    for row in attributions:
        attr = row.attribution
        algebra = attr.regt_marginal_consumption - attr.pm_marginal_consumption
        if abs(attr.regt_excess_over_pm - algebra) > _TOL:
            failures.append(
                f"regt_excess_over_pm algebra mismatch for fill_id={row.fill_id!r}: "
                f"{attr.regt_excess_over_pm} != {algebra} (tol={_TOL})"
            )

    # ALP-462 — ``regt_excess_over_pm`` is ``Money`` (Decimal); the aggregator
    # trailing-window values are still float for now. Compare in Decimal space
    # so the precision invariant holds across the Decimal sum.
    expected_sum = sum(
        (row.attribution.regt_excess_over_pm for row in attributions),
        start=Decimal(0),
    )
    for label, value in (
        ("trailing-30d", trailing_30d_usd),
        ("trailing-90d", trailing_90d_usd),
        ("lifetime", lifetime_usd),
    ):
        if value is None:
            continue
        if abs(Decimal(str(value)) - expected_sum) <= Decimal(str(_TOL)):
            continue
        failures.append(
            f"{label} aggregate {value} does not equal sum of per-fill "
            f"regt_excess_over_pm {expected_sum} (tol={_TOL}); "
            f"see runbook § Failure-mode triage row 'trailing-30d aggregate mismatch'"
        )

    return tuple(failures)


def _all_fields_finite(attribution: RegTMarginAttribution) -> bool:
    """Every numeric attribution field is finite (no NaN, no inf)."""
    numeric_fields = (
        attribution.regt_margin_before,
        attribution.regt_margin_after,
        attribution.regt_marginal_consumption,
        attribution.pm_equivalent_before,
        attribution.pm_equivalent_after,
        attribution.pm_marginal_consumption,
        attribution.regt_excess_over_pm,
    )
    return all(math.isfinite(v) for v in numeric_fields)


# ---------------------------------------------------------------------------
# Verify orchestrator
# ---------------------------------------------------------------------------


async def run_verify(db_path: Path, *, invocation_id: str) -> VerifyResult:
    """End-to-end verification entry point. Closes the engine before returning."""
    engine, factory = _open_async_factory(db_path)
    try:
        await seed_portfolio_and_fills(factory)
        await _run_phase1_drain(factory, invocation_id=invocation_id)
        attributions = await rehydrate_attributions(factory)
        cash = await _run_assembler_for_aggregates(factory, invocation_id=invocation_id)
        failures = _collect_assertion_failures(
            attributions,
            trailing_30d_usd=cash.regt_excess_trailing_30d_usd,
            trailing_90d_usd=cash.regt_excess_trailing_90d_usd,
            lifetime_usd=cash.regt_excess_lifetime_usd,
        )
        return VerifyResult(
            attributions=attributions,
            trailing_30d_usd=cash.regt_excess_trailing_30d_usd,
            trailing_90d_usd=cash.regt_excess_trailing_90d_usd,
            lifetime_usd=cash.regt_excess_lifetime_usd,
            failures=failures,
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_per_fill_block(row: AttributionRow) -> Iterable[str]:
    attr = row.attribution
    yield f"  Fill {row.fill_id}:"
    yield f"      regt_margin_before          = {attr.regt_margin_before:>14.4f}"
    yield f"      regt_margin_after           = {attr.regt_margin_after:>14.4f}"
    yield f"      regt_marginal_consumption   = {attr.regt_marginal_consumption:>14.4f}"
    yield f"      pm_equivalent_before        = {attr.pm_equivalent_before:>14.4f}"
    yield f"      pm_equivalent_after         = {attr.pm_equivalent_after:>14.4f}"
    yield f"      pm_marginal_consumption     = {attr.pm_marginal_consumption:>14.4f}"
    yield f"      regt_excess_over_pm         = {attr.regt_excess_over_pm:>14.4f}"
    yield f"      pm_model_version            = {attr.pm_model_version}"


def _render_text(result: VerifyResult) -> str:
    lines = ["=" * 70, "AlphaMind Reg T Margin Attribution Verification", "=" * 70]
    if not result.attributions:
        lines.append("  (no processed fills with regt_attribution_json found)")
    else:
        lines.append("Per-fill RegTMarginAttribution:")
        for row in result.attributions:
            lines.extend(_render_per_fill_block(row))
    lines.append("-" * 70)
    lines.append("Trailing-window aggregates (from assembled CashLedger):")
    lines.append(f"  regt_excess_trailing_30d_usd = {result.trailing_30d_usd:>14.4f}")
    lines.append(f"  regt_excess_trailing_90d_usd = {result.trailing_90d_usd:>14.4f}")
    lines.append(f"  regt_excess_lifetime_usd     = {result.lifetime_usd:>14.4f}")
    lines.append("=" * 70)
    if result.ok:
        lines.append("PASS")
    else:
        lines.append("FAIL")
        for failure in result.failures:
            lines.append(f"  - {failure}")
    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the Reg T margin attribution work tree end-to-end against a "
            "freshly-migrated SQLite DB: seed a representative four-position "
            "portfolio + two unprocessed fills, drive process_unprocessed_fills, "
            "rehydrate per-fill RegTMarginAttribution, assert the algebra, and "
            "compare to the assembler's trailing-30d aggregate. "
            "Loads .env via `source .env` before invocation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=(
            "Path to a freshly-migrated SQLite DB. When omitted, the script creates a "
            "temp directory and migrates a fresh DB to head."
        ),
    )
    parser.add_argument(
        "--invocation-id",
        default=_DEFAULT_INVOCATION_ID,
        help=f"Invocation id stamped on the seeded run (default: {_DEFAULT_INVOCATION_ID}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the resolved DB path and config snapshot before running.",
    )
    return parser


def _resolve_db_path(arg_db_path: Path | None) -> Path:
    if arg_db_path is not None:
        return arg_db_path
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="verify-regt-margin-"))
    db_path = tmp / "alphamind.db"
    _migrate_db_to_head(db_path)
    return db_path


def _migrate_db_to_head(db_path: Path) -> None:
    """Apply the state-persistence schema via ``Base.metadata.create_all``."""
    import alphamind.state.tables  # noqa: F401
    from alphamind.persistence.models import Base

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()


def _print_verbose_preamble(args: argparse.Namespace, db_path: Path) -> None:
    """Emit a one-block preamble naming the resolved DB + config snapshot."""
    config = load_regt_margin_attribution_config()
    payload: dict[str, Any] = {
        "db_path": str(db_path),
        "invocation_id": args.invocation_id,
        "pm_model_version": config.pm_model_version,
        "risk_free_rate_annual": config.risk_free_rate_annual,
    }
    print("verify_regt_margin_attribution: " + json.dumps(payload))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on PASS, 1 on FAIL."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.db_path)
    if args.verbose:
        _print_verbose_preamble(args, db_path)

    result = asyncio.run(run_verify(db_path, invocation_id=args.invocation_id))
    print(_render_text(result))
    return EXIT_PASS if result.ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
