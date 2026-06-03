"""Tests for the atomicity-first per-command write path (ALP-836).

``precommit_command`` (A) / ``backfill_command_broker_ids`` (C) /
``abandon_command`` (F) each run in their own committed transaction on a fresh
session, so a durable ``orders`` row keyed by ``client_order_id`` exists before
the broker dispatch and a lost post-submit commit can never strand a live broker
order. Reuses the phase2 write-path builders + the shared ``db`` fixture.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.money import price
from alphamind.commands.command_models import EntryOrder, OpenCommand
from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult
from alphamind.execution.write_paths.phase2.atomic import (
    abandon_command,
    backfill_command_broker_ids,
    dispatched_order_id,
    precommit_command,
)
from alphamind.portfolio_state.records.orders import OrderRole, OrderStatus
from alphamind.state.tables.cash_ledger import CASH_LEDGER_SINGLETON_ID, CashLedgerRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from tests.execution.state_persistence.test_phase2_write_path import (
    _accepted_result,
    _active_bracket,
    _active_thesis,
    _close_command,
    _open_command,
    _open_position,
    _seed_cash_ledger,
    _seed_invocation_substrate,
    _seed_position_cluster,
)

_INV = "inv-2026-05-08T12:00:00Z-aaaa"


def _dispatch_result(
    alpaca_order_id: str,
    *,
    leg_alpaca_order_ids: dict[str, str] | None = None,
    payload_kind: str = "equity",
) -> BrokerDispatchResult:
    return BrokerDispatchResult(
        alpaca_order_id=alpaca_order_id,  # type: ignore[arg-type]
        client_order_id="cid",  # type: ignore[arg-type]
        status="accepted",
        order_class="simple",
        payload_kind=payload_kind,  # type: ignore[arg-type]
        raw_submission=None,
        leg_alpaca_order_ids=leg_alpaca_order_ids or {},  # type: ignore[arg-type]
    )


async def _read_order_by_client_order_id(
    factory: async_sessionmaker[AsyncSession], client_order_id: str
) -> OrderRow | None:
    async with factory() as sess:
        return (
            await sess.execute(select(OrderRow).where(OrderRow.client_order_id == client_order_id))
        ).scalar_one_or_none()


async def _read_reserved_capital(factory: async_sessionmaker[AsyncSession]) -> float:
    async with factory() as sess:
        row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert row is not None
        return float(row.reserved_capital_usd)


async def _seed_open_close_substrate(factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed_invocation_substrate(factory, invocation_id=_INV)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())


def _limit_open() -> OpenCommand:
    """An OPEN with a LIMIT entry so it reserves capital (market reserves nothing)."""
    return _open_command(
        entry_order=EntryOrder(type="limit", limit_price=price(900.0), stop_price=None)
    )


# ---------------------------------------------------------------------------
# (A) pre-commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_precommit_close_writes_pending_submit_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_open_close_substrate(factory)
    cmd = _close_command()
    result = _accepted_result(0, "inv-X.ENV-SA-1.0.0")

    landed = await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    assert landed is True
    row = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-1.0.0")
    assert row is not None
    assert row.status == OrderStatus.PENDING_SUBMIT.value
    assert row.order_role == OrderRole.CLOSE.value
    # Synthetic placeholder until backfill — never a real broker id pre-dispatch.
    assert row.alpaca_order_id.startswith("alp-")


@pytest.mark.asyncio
async def test_precommit_open_reserves_capital_and_marks_entry_pending_submit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_invocation_substrate(factory, invocation_id=_INV)
    await _seed_cash_ledger(factory)
    cmd = _limit_open()
    result = _accepted_result(0, "inv-X.ENV-SA-2.0.0")

    landed = await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    assert landed is True
    row = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-2.0.0")
    assert row is not None
    assert row.order_role == OrderRole.ENTRY.value
    assert row.status == OrderStatus.PENDING_SUBMIT.value
    # 10 shares * $900 limit = $9000 reserved.
    assert await _read_reserved_capital(factory) == pytest.approx(9000.0)


@pytest.mark.asyncio
async def test_precommit_is_idempotent_no_double_reservation(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_invocation_substrate(factory, invocation_id=_INV)
    await _seed_cash_ledger(factory)
    cmd = _limit_open()
    result = _accepted_result(0, "inv-X.ENV-SA-3.0.0")

    assert await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)
    # Replay — must be a no-op: one row, one reservation.
    assert await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(OrderRow).where(OrderRow.client_order_id == "inv-X.ENV-SA-3.0.0")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert await _read_reserved_capital(factory) == pytest.approx(9000.0)


# ---------------------------------------------------------------------------
# (C) backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_flips_to_pending_and_stamps_real_id(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_open_close_substrate(factory)
    cmd = _close_command()
    result = _accepted_result(0, "inv-X.ENV-SA-4.0.0")
    await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    await backfill_command_broker_ids(
        factory,
        command=cmd,
        result=result,
        dispatch_result=_dispatch_result("real-uuid-1"),
    )

    row = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-4.0.0")
    assert row is not None
    assert row.status == OrderStatus.PENDING.value
    assert row.alpaca_order_id == "real-uuid-1"
    assert row.alpaca_order_id_chain_json == '["real-uuid-1"]'


@pytest.mark.asyncio
async def test_backfill_open_stamps_native_bracket_leg_ids(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_invocation_substrate(factory, invocation_id=_INV)
    await _seed_cash_ledger(factory)
    cmd = _limit_open()
    result = _accepted_result(0, "inv-X.ENV-SA-5.0.0")
    await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    await backfill_command_broker_ids(
        factory,
        command=cmd,
        result=result,
        dispatch_result=_dispatch_result(
            "entry-uuid",
            leg_alpaca_order_ids={"take_profit": "tp-uuid", "stop_loss": "sl-uuid"},
        ),
    )

    entry = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-5.0.0")
    assert entry is not None
    bracket_id = entry.bracket_id
    async with factory() as sess:
        legs = {
            r.order_role: r.alpaca_order_id
            for r in (
                await sess.execute(select(OrderRow).where(OrderRow.bracket_id == bracket_id))
            ).scalars()
        }
    assert legs[OrderRole.TAKE_PROFIT.value] == "tp-uuid"
    assert legs[OrderRole.PRICE_STOP.value] == "sl-uuid"


# ---------------------------------------------------------------------------
# (F) abandon / teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abandon_close_cancels_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_open_close_substrate(factory)
    cmd = _close_command()
    result = _accepted_result(0, "inv-X.ENV-SA-6.0.0")
    await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    await abandon_command(
        factory, invocation_id=_INV, command=cmd, result=result, reason="broker_gateway_failure"
    )

    row = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-6.0.0")
    assert row is not None
    assert row.status == OrderStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_abandon_open_tears_down_graph_and_releases_capital(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_invocation_substrate(factory, invocation_id=_INV)
    await _seed_cash_ledger(factory)
    cmd = _limit_open()
    result = _accepted_result(0, "inv-X.ENV-SA-7.0.0")
    await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)
    assert await _read_reserved_capital(factory) == pytest.approx(9000.0)

    await abandon_command(
        factory, invocation_id=_INV, command=cmd, result=result, reason="broker_gateway_failure"
    )

    entry = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-7.0.0")
    assert entry is not None
    assert entry.status == OrderStatus.CANCELLED.value
    # No phantom open: the never-filled position is driven terminal, capital freed.
    async with factory() as sess:
        pos = await sess.get(PositionRow, entry.position_id)
        assert pos is not None
        assert pos.status == "CANCELLED"
    assert await _read_reserved_capital(factory) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_abandon_missing_precommit_is_noop(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A-before-B: if the pre-commit never landed, nothing reached the broker and
    abandon is a clean no-op (no row, no negative capital)."""
    _, factory = db
    await _seed_open_close_substrate(factory)
    cmd = _close_command()
    result = _accepted_result(0, "inv-X.ENV-SA-8.0.0")

    await abandon_command(
        factory, invocation_id=_INV, command=cmd, result=result, reason="never_dispatched"
    )

    assert await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-8.0.0") is None


def test_dispatched_order_id_is_none_for_cancel() -> None:
    from alphamind._kernel.ids import OrderId
    from alphamind.commands.command_models import CancelCommand

    cancel = CancelCommand(command_type="cancel", order_id=OrderId("ord-x"), cancel_reason="stale")
    assert dispatched_order_id(cancel, command_id="inv-X.ENV-SA-9.0.0") is None


# ---------------------------------------------------------------------------
# (E) reconcile order→broker-link backfill (lost-(C) recovery)
# ---------------------------------------------------------------------------


def _order_snapshot(*, order_id: str, client_order_id: str, status: str = "filled") -> object:
    from datetime import UTC, datetime

    from alphamind.execution.broker_adapter.queries import OrderSnapshot

    return OrderSnapshot(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol="NVDA",
        asset_class="us_equity",
        qty=10.0,
        filled_qty=10.0 if status == "filled" else 0.0,
        side="sell",
        order_type="market",
        time_in_force="day",
        order_class="simple",
        status=status,
        submitted_at=datetime(2026, 6, 3, tzinfo=UTC),
        filled_at=None,
        replaced_by=None,
        replaces=None,
        legs=None,
    )


@pytest.mark.asyncio
async def test_backfill_pending_submit_orders_recovers_lost_link(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-836 (E) — a PENDING_SUBMIT row whose post-submit backfill was lost is
    recovered: matched to the live Alpaca order by client_order_id, stamped with
    the real alpaca_order_id, and flipped PENDING_SUBMIT → PENDING."""
    from alphamind.execution.corporate_actions.reconciliation import (
        backfill_pending_submit_orders,
    )
    from alphamind.state.invocation_context.context import InvocationHandle

    _, factory = db
    await _seed_open_close_substrate(factory)
    cmd = _close_command()
    result = _accepted_result(0, "inv-X.ENV-SA-E.0.0")
    # Pre-commit but NEVER backfill — the row is stuck in PENDING_SUBMIT with the
    # synthetic placeholder (the lost-(C) state).
    await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)
    row = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-E.0.0")
    assert row is not None
    assert row.status == OrderStatus.PENDING_SUBMIT.value
    assert row.alpaca_order_id.startswith("alp-")

    snapshot = _order_snapshot(order_id="real-broker-uuid", client_order_id="inv-X.ENV-SA-E.0.0")
    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INV)
        n = await backfill_pending_submit_orders(handle, alpaca_orders=(snapshot,))  # type: ignore[arg-type]
        await session.commit()
    assert n == 1

    row = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-E.0.0")
    assert row is not None
    assert row.alpaca_order_id == "real-broker-uuid"
    assert row.status == OrderStatus.PENDING.value


@pytest.mark.asyncio
async def test_backfill_pending_submit_orders_noop_without_match(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A PENDING_SUBMIT row with no matching Alpaca order is left untouched (the
    dispatch likely never landed) — no spurious id stamp."""
    from alphamind.execution.corporate_actions.reconciliation import (
        backfill_pending_submit_orders,
    )
    from alphamind.state.invocation_context.context import InvocationHandle

    _, factory = db
    await _seed_open_close_substrate(factory)
    cmd = _close_command()
    result = _accepted_result(0, "inv-X.ENV-SA-F.0.0")
    await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    other = _order_snapshot(order_id="unrelated", client_order_id="some-other-command")
    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INV)
        n = await backfill_pending_submit_orders(handle, alpaca_orders=(other,))  # type: ignore[arg-type]
        await session.commit()
    assert n == 0

    row = await _read_order_by_client_order_id(factory, "inv-X.ENV-SA-F.0.0")
    assert row is not None
    assert row.status == OrderStatus.PENDING_SUBMIT.value
    assert row.alpaca_order_id.startswith("alp-")


# ---------------------------------------------------------------------------
# Integrity guard — phase2 stamp withheld on a PENDING_SUBMIT strand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_has_pending_submit_strand_detects_and_scopes(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-836 — the integrity guard flags THIS invocation's stuck PENDING_SUBMIT
    row (a lost post-submit backfill), is scoped to its command_id prefix, and
    clears once the row is backfilled to PENDING."""
    from alphamind.execution.write_paths.phase2.atomic import (
        invocation_has_pending_submit_strand,
    )

    _, factory = db
    await _seed_open_close_substrate(factory)
    # A command_id whose prefix matches the invocation (inv-{invocation_id}.…).
    command_id = f"{_INV}.ENV-SA-1.0.0"
    result = _accepted_result(0, command_id)
    await precommit_command(factory, invocation_id=_INV, command=_close_command(), result=result)

    async with factory() as session:
        assert await invocation_has_pending_submit_strand(session, invocation_id=_INV) is True
        # A different invocation's stamp is unaffected by this strand.
        assert (
            await invocation_has_pending_submit_strand(
                session, invocation_id="inv-2099-01-01T00:00:00Z-zzzz"
            )
            is False
        )

    # Once backfilled to PENDING, the strand is cleared.
    await backfill_command_broker_ids(
        factory,
        command=_close_command(),
        result=result,
        dispatch_result=_dispatch_result("real-uuid"),
    )
    async with factory() as session:
        assert await invocation_has_pending_submit_strand(session, invocation_id=_INV) is False
