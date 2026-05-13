"""Tests for the Phase 1 Reg T margin attribution wedge (story 06a / ALP-428).

The wedge in ``process_unprocessed_fills`` snapshots positions before and
after each fill is integrated, calls ``compute_attribution`` to produce the
eight-field ``RegTMarginAttribution`` record, and persists the serialized
record into the fill row's ``regt_attribution_json`` column. Quarantined
fills are skipped — they retain ``regt_attribution_json IS NULL``.

Tests use the same engine + builder helpers as ``test_phase1_write_path.py``
but exercise the wedge-specific behaviour: per-fill attribution presence,
quarantined-fill nullability, deterministic batched-fill threading, and the
required-MarketInputs surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    Symbol,
)
from alphamind._kernel.regime import RiskZone
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationContext,
    InvocationHandle,
)
from alphamind.execution.state_persistence.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.execution.state_persistence.tables.fill_records import FillRecordRow
from alphamind.execution.state_persistence.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.execution.state_persistence.write_paths.fill_persistence import (
    append_fill_record,
)
from alphamind.execution.state_persistence.write_paths.records import (
    FillProcessingStatus,
    FillRecord,
    RegTMarginAttribution,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import (
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
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionRecord,
    PositionStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-regt"
_PROCESS_ID = "proc-regt-1"
_TICKER = "AAPL"
_SPOT = 150.0


# ---------------------------------------------------------------------------
# Engine + session fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_process_lifetime() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-regt-1.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0",
    )


def _make_invocation_record(invocation_id: str = _INV_ID) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/provenance/inv/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/provenance/calibration.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


_BRACKET_ID = "brk-regt-1"
_POSITION_ID = "pos-1"


def _make_pending_entry_order(
    order_id: str = "ord-entry-1",
    *,
    quantity: float = 10.0,
    direction: OrderDirection = OrderDirection.BUY,
    ticker: str = _TICKER,
    position_id: str = _POSITION_ID,
) -> OrderRecord:
    """Order with ``position_id`` already set so the wedge resolves the
    position directly (skipping the bracket lookup that the entry-fill
    happy-path normally walks)."""
    return OrderRecord.model_validate(
        {
            "order_id": order_id,
            "position_id": position_id,
            "bracket_id": _BRACKET_ID,
            "role": OrderRole.ENTRY,
            "instrument_spec": EquityInstrumentSpec(ticker=Symbol(ticker)),
            "direction": direction,
            "order_type": OrderType.MARKET,
            "order_class": OrderClass.SIMPLE,
            "price_parameters": PriceParameters(),
            "quantity": quantity,
            "duration": OrderDuration.DAY,
            "status": OrderStatus.PENDING,
            "alpaca_order_id": f"alp-{order_id}",
            "alpaca_order_id_chain": (f"alp-{order_id}",),
            "submission_timestamp": _NOW - timedelta(minutes=15),
            "last_update_timestamp": _NOW - timedelta(minutes=15),
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": quantity,
            "modification_count": 0,
            "originating_thesis_id": None,
            "originating_pm_command_id": None,
            "age_hours": 0.25,
        }
    )


def _make_pending_position(
    position_id: str = _POSITION_ID,
    *,
    ticker: str = _TICKER,
) -> PositionRecord:
    """Pending position with ``bracket_id=None`` — the wedge skips bracket
    transitions entirely when the position carries no bracket_id."""
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=0.0,
        average_cost_basis_per_share=0.0,
    )
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.PENDING,
            "direction": Direction.LONG,
            "entry_timestamp": None,
            "details": details,
            "execution_history": (),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_cash_ledger(current_cash_usd: float = 100_000.0) -> CashLedger:
    return CashLedger.model_validate(
        {
            "current_cash_usd": current_cash_usd,
            "settled_cash_usd": current_cash_usd,
            "reserved_capital_usd": 0.0,
            "available_buying_power_usd": current_cash_usd,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": 0.0,
            "equity_high_water_mark_usd": 100_000.0,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": 0.0,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


def _make_unprocessed_fill(
    fill_id: str,
    *,
    order_id: str = "ord-entry-1",
    fill_quantity: float = 10.0,
    fill_price: float = _SPOT,
    fill_timestamp: datetime | None = None,
    remaining_quantity_after: float = 0.0,
    order_status_after: OrderStatus = OrderStatus.FILLED,
) -> FillRecord:
    ts = fill_timestamp if fill_timestamp is not None else _NOW - timedelta(minutes=10)
    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        fill_timestamp=ts,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        remaining_quantity_after=remaining_quantity_after,
        order_status_after=order_status_after,
        slippage_usd=0.0,
        fees_usd=0.0,
        execution_venue="NASDAQ",
        gateway_reference=f"alp-{fill_id}",
        persistence_timestamp=ts + timedelta(seconds=1),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


def _make_market_inputs(underlying_prices: dict[str, float]) -> MarketInputs:
    """Return a minimal ``MarketInputs`` with an empty IV provider.

    The wedge's pre-/post-fill snapshots in these tests use only equity
    positions, so the IV provider is never consulted; an empty
    ``FixtureIvProvider`` suffices.
    """
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=0.0425,
        iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        as_of=_NOW,
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_invocation_substrate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(_make_invocation_record()))
        await sess.commit()


async def _seed_position_order_bracket(
    factory: async_sessionmaker[AsyncSession],
    *,
    position: PositionRecord,
    order: OrderRecord,
) -> None:
    """Insert position + bracket + order in a single deferred-FK transaction.

    The order references the bracket; the bracket references the position
    and the entry-order. They all must commit together.
    """
    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(order_record_to_row(order))
        sess.add(
            BracketRow(
                bracket_id=_BRACKET_ID,
                position_id=position.position_id,
                status=BracketStatus.PENDING_ENTRY.value,
                entry_order_id=order.order_id,
                entry_window_deadline=None,
                corporate_action_cancellation_reason=None,
                modification_history_json="[]",
            )
        )
        await sess.commit()


async def _seed_cash_ledger(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as sess:
        sess.add(cash_ledger_record_to_row(_make_cash_ledger(), last_updated_at=_NOW))
        await sess.commit()


async def _seed_drawdown_state(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as sess:
        sess.add(drawdown_state_record_to_row(_make_drawdown_state(), last_updated_at=_NOW))
        await sess.commit()


async def _append_fill(
    factory: async_sessionmaker[AsyncSession],
    fill: FillRecord,
) -> None:
    async with factory() as sess:
        await append_fill_record(sess, fill)
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id_suffix: str = "phase1",
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=f"{_INV_ID}-{invocation_id_suffix}"),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


async def _seed_minimal_substrate(
    factory: async_sessionmaker[AsyncSession],
    *,
    position: PositionRecord,
    order: OrderRecord,
) -> None:
    """Seed the minimum substrate the wedge needs.

    Inserts: invocation row pair, position + order + bracket cluster, cash
    ledger singleton, drawdown state singleton.
    """
    await _seed_invocation_substrate(factory)
    await _seed_position_order_bracket(factory, position=position, order=order)
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)


def _state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/pip-freeze",
            "invocation_provenance_root": "/tmp/provenance",
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_processed_fill_carries_populated_attribution(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A processed fill's regt_attribution_json round-trips to a non-null
    eight-field RegTMarginAttribution with all numeric fields finite."""
    import math

    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_minimal_substrate(
        factory,
        position=_make_pending_position(),
        order=_make_pending_entry_order(),
    )
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs({_TICKER: _SPOT}),
        config=_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 0

    async with factory() as sess:
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-1"))
        ).scalar_one()
        assert fill_row.regt_attribution_json is not None
        attribution = RegTMarginAttribution.model_validate_json(fill_row.regt_attribution_json)
        for value in (
            attribution.regt_margin_before,
            attribution.regt_margin_after,
            attribution.regt_marginal_consumption,
            attribution.pm_equivalent_before,
            attribution.pm_equivalent_after,
            attribution.pm_marginal_consumption,
            attribution.regt_excess_over_pm,
        ):
            assert math.isfinite(value)
        assert attribution.pm_model_version  # non-empty version pinned to the snapshot


async def test_quarantined_fill_retains_null_attribution(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill rejected by ``_quarantine_invalid`` (e.g., negative quantity)
    is excluded from integration and its ``regt_attribution_json`` stays NULL."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_minimal_substrate(
        factory,
        position=_make_pending_position(),
        order=_make_pending_entry_order(),
    )
    # Quantity 0.0 fails the ``> 0`` check in _quarantine_invalid.
    await _append_fill(
        factory,
        _make_unprocessed_fill(fill_id="fill-bad", fill_quantity=0.0),
    )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs({_TICKER: _SPOT}),
        config=_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 0
    assert summary.fills_quarantined == 1

    async with factory() as sess:
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-bad"))
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.QUARANTINED.value
        assert fill_row.regt_attribution_json is None


async def test_batched_fills_have_threaded_pre_state(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Two unprocessed fills on the same underlying are processed in
    timestamp order. Fill 2's ``regt_margin_before`` equals fill 1's
    ``regt_margin_after`` — the wedge re-snapshots positions per fill so
    later fills see the cumulative effect of earlier ones.
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_minimal_substrate(
        factory,
        position=_make_pending_position(),
        order=_make_pending_entry_order(quantity=10.0),
    )
    earlier = _NOW - timedelta(minutes=20)
    later = _NOW - timedelta(minutes=10)
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-1-earlier",
            fill_quantity=4.0,
            fill_price=_SPOT,
            fill_timestamp=earlier,
            remaining_quantity_after=6.0,
            order_status_after=OrderStatus.PARTIALLY_FILLED,
        ),
    )
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-2-later",
            fill_quantity=6.0,
            fill_price=_SPOT,
            fill_timestamp=later,
            remaining_quantity_after=0.0,
            order_status_after=OrderStatus.FILLED,
        ),
    )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs({_TICKER: _SPOT}),
        config=_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 2

    async with factory() as sess:
        fill1_row = (
            await sess.execute(
                select(FillRecordRow).where(FillRecordRow.fill_id == "fill-1-earlier")
            )
        ).scalar_one()
        fill2_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-2-later"))
        ).scalar_one()
        assert fill1_row.regt_attribution_json is not None
        assert fill2_row.regt_attribution_json is not None
        attribution1 = RegTMarginAttribution.model_validate_json(fill1_row.regt_attribution_json)
        attribution2 = RegTMarginAttribution.model_validate_json(fill2_row.regt_attribution_json)

    # Determinism: fill 2's pre-state is exactly fill 1's post-state.
    assert attribution2.regt_margin_before == pytest.approx(attribution1.regt_margin_after)
    assert attribution2.pm_equivalent_before == pytest.approx(attribution1.pm_equivalent_after)
    # Sanity: both fills consumed margin (long-equity buy increases requirement).
    assert attribution1.regt_marginal_consumption > 0.0
    assert attribution2.regt_marginal_consumption > 0.0


async def test_phase1_summary_unchanged(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The wedge does not alter ``Phase1Summary`` — the four count fields are
    still populated and remain the function's only return surface."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        Phase1Summary,
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_minimal_substrate(
        factory,
        position=_make_pending_position(),
        order=_make_pending_entry_order(),
    )
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs({_TICKER: _SPOT}),
        config=_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert isinstance(summary, Phase1Summary)
    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 0
    assert summary.ca_activities_processed == 0
    # ``reconciliation_alerts`` may be non-zero because no Alpaca snapshot was
    # passed; what matters here is the field still exists and is an int.
    assert isinstance(summary.reconciliation_alerts, int)
    # No additional fields snuck in via the wedge.
    assert {f.name for f in summary.__dataclass_fields__.values()} == {
        "fills_processed",
        "fills_quarantined",
        "ca_activities_processed",
        "reconciliation_alerts",
    }


async def test_missing_market_inputs_for_underlying_propagates_key_error(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When ``market_inputs.underlying_prices`` does not cover an open
    position's underlying, the wedge propagates ``KeyError`` from
    :func:`compute_attribution`.

    The caller (in production: the continuous monitor) is responsible for
    populating prices for every open-position underlying before invoking
    Phase 1. Surfacing the gap as a hard error prevents silent attribution
    misreporting.
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_minimal_substrate(
        factory,
        position=_make_pending_position(),
        order=_make_pending_entry_order(),
    )
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1"))

    ctx, handle = await _open_handle(factory)
    with pytest.raises(KeyError):
        # ``underlying_prices`` empty — the wedge cannot price the position.
        await process_unprocessed_fills(
            handle,
            market_inputs=_make_market_inputs({}),
            config=_state_persistence_config(),
        )
    await ctx.__aexit__(None, None, None)
