"""Tests for the fill collection Reg T margin attribution wedge (story 06a / ALP-428).

The wedge in ``process_unprocessed_fills`` snapshots positions before and
after each fill is integrated, calls ``compute_attribution`` to produce the
eight-field ``RegTMarginAttribution`` record, and persists the serialized
record into the fill row's ``regt_attribution_json`` column. Quarantined
fills are skipped — they retain ``regt_attribution_json IS NULL``.

Tests use the same engine + builder helpers as ``test_fill_collection_write_path.py``
but exercise the wedge-specific behaviour: per-fill attribution presence,
quarantined-fill nullability, deterministic batched-fill threading, and the
required-MarketInputs surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.write_paths.fill_persistence import (
    append_fill_record,
)
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
    LocateStatus,
    PositionRecord,
    PositionStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.context import (
    InvocationContext,
    InvocationHandle,
)
from alphamind.state.invocation_context.records import (
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
    RegTMarginAttribution,
)
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from tests.execution.state_persistence.conftest import (
    _make_invocation_record,
    _make_process_lifetime,
    _seed_cash_ledger,
    _seed_drawdown_state,
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
# Builders
# ---------------------------------------------------------------------------


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
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId(_BRACKET_ID),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(ticker)),
        direction=direction,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=quantity,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=quantity,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def _make_pending_position(
    position_id: str = _POSITION_ID,
    *,
    ticker: str = _TICKER,
    direction: Direction = Direction.LONG,
) -> PositionRecord:
    """Pending position with ``bracket_id=None`` — the wedge skips bracket
    transitions entirely when the position carries no bracket_id.

    SHORT positions get the four short-only fields stamped at their PENDING
    skeleton defaults (zeroed numeric fields, LOCATED locate status).
    """
    is_short = direction == Direction.SHORT
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=0.0,
        average_cost_basis_per_share=0.0,
        borrow_rate_pct=0.0 if is_short else None,
        accrued_borrow_cost_usd=0.0 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=0.0 if is_short else None,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING,
        direction=direction,
        entry_timestamp=None,
        details=details,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
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
        fill_price=price(fill_price),
        fill_quantity=fill_quantity,
        remaining_quantity_after=remaining_quantity_after,
        order_status_after=order_status_after,
        slippage_usd=signed_money(0.0),
        fees_usd=money(0.0),
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
        sess.add(
            process_lifetime_record_to_row(
                _make_process_lifetime(
                    process_lifetime_id=_PROCESS_ID,
                    pip_freeze_snapshot_path="/tmp/pip-freeze/proc-regt-1.txt",
                )
            )
        )
        await sess.flush()
        sess.add(
            invocation_record_to_row(
                _make_invocation_record(
                    invocation_id=_INV_ID,
                    process_lifetime_id=_PROCESS_ID,
                )
            )
        )
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
    invocation_id_suffix: str = "fill_collection",
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(
            invocation_id=f"{_INV_ID}-{invocation_id_suffix}",
            process_lifetime_id=_PROCESS_ID,
        ),
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

    from alphamind.execution.write_paths.fill_collection import (
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


async def test_short_entry_fill_attribution_carries_150pct_initial_margin(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A SHORT-equity entry fill's Reg T attribution reflects the
    short-equity 150%-MV initial margin formula: ``regt_marginal_consumption
    = 1.50 * fill_quantity * fill_price``.

    The wedge snapshots positions pre- and post-fill, calls
    ``compute_attribution`` (which folds short-equity positions into the
    150%-MV bucket per
    ``regt_margin.py::_SHORT_EQUITY_INITIAL_MARGIN_PCT``), and writes the
    delta into ``regt_marginal_consumption`` (ALP-717)."""
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_minimal_substrate(
        factory,
        position=_make_pending_position(direction=Direction.SHORT),
        order=_make_pending_entry_order(direction=OrderDirection.SELL_TO_OPEN),
    )
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-short-1"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs({_TICKER: _SPOT}),
        config=_state_persistence_config(),
        borrow_cost_resolver=lambda _t: 15.0,
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1

    async with factory() as sess:
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-short-1"))
        ).scalar_one()
        assert fill_row.regt_attribution_json is not None
        attribution = RegTMarginAttribution.model_validate_json(fill_row.regt_attribution_json)
        # Pre-fill: PENDING SHORT position has share_count=0 → 0 Reg T margin.
        assert attribution.regt_margin_before == pytest.approx(0.0)
        # Post-fill: 10 shares short at $150 → 1.50 * 10 * 150 = 2250.
        assert attribution.regt_margin_after == pytest.approx(1.50 * 10.0 * 150.0)
        assert attribution.regt_marginal_consumption == pytest.approx(1.50 * 10.0 * 150.0)


async def test_quarantined_fill_retains_null_attribution(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill rejected by ``_quarantine_invalid`` (e.g., negative quantity)
    is excluded from integration and its ``regt_attribution_json`` stays NULL."""
    from alphamind.execution.write_paths.fill_collection import (
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
    from alphamind.execution.write_paths.fill_collection import (
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


async def test_fill_collection_summary_unchanged(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The wedge does not alter ``FillCollectionSummary`` — the four count fields are
    still populated and remain the function's only return surface."""
    from alphamind.execution.write_paths.fill_collection import (
        FillCollectionSummary,
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

    assert isinstance(summary, FillCollectionSummary)
    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 0
    assert summary.ca_activities_processed == 0
    # ``reconciliation_alerts`` may be non-zero because no Alpaca snapshot was
    # passed; what matters here is the field still exists and is an int.
    assert isinstance(summary.reconciliation_alerts, int)
    # No additional fields snuck in via the Reg-T wedge. ``projection_rebuild``
    # is the W2a projection-rebuild return surface (ALP-854), not the wedge.
    assert {f.name for f in summary.__dataclass_fields__.values()} == {
        "fills_processed",
        "fills_quarantined",
        "ca_activities_processed",
        "reconciliation_alerts",
        "projection_rebuild",
    }


async def test_missing_market_inputs_for_underlying_propagates_key_error(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When ``market_inputs.underlying_prices`` does not cover an open
    position's underlying, the wedge propagates ``KeyError`` from
    :func:`compute_attribution`.

    The caller (in production: the continuous monitor) is responsible for
    populating prices for every open-position underlying before invoking
    fill collection. Surfacing the gap as a hard error prevents silent attribution
    misreporting.
    """
    from alphamind.execution.write_paths.fill_collection import (
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
