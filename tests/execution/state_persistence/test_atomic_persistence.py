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

from alphamind._kernel.money import money, price
from alphamind.commands.command_models import (
    BracketOrderParameters,
    CapitalProtectionFloor,
    EntryOrder,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
)
from alphamind.commands.command_models import (
    ThesisComponent as OMSThesisComponent,
)
from alphamind.commands.submission_results import (
    Acknowledgment,
    SubmissionResult,
    _ValidationMetadata,
)
from alphamind.execution.broker_adapter.order_options import (
    derive_capital_floor_client_order_id,
)
from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult
from alphamind.execution.oms.command_ids import derive_open_thesis_id, derive_pm_command_id
from alphamind.execution.write_paths.phase2.atomic import (
    abandon_command,
    backfill_command_broker_ids,
    dispatched_order_id,
    precommit_command,
)
from alphamind.execution.write_paths.phase2.open import _capital_floor_order_id
from alphamind.portfolio_state.records.orders import OrderRole, OrderStatus
from alphamind.risk_guardrails.guardrail_evaluation import Greeks
from alphamind.state.tables.bracket_legs import BracketLegRow
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


def _open_cid(envelope_id: str, *, ticker: str = "NVDA") -> str:
    """A realistic broker-carried OPEN command id (ALP-844) for ``_limit_open``.

    Phase-2 OPEN writeback (reached here via ``precommit_command`` →
    ``dispatched_order_id`` → ``_new_open_ids``) resolves the thesis by parsing
    the command id, so an OPEN's pre-commit must carry a thesis-bearing PM id.
    Mints the thesis off the link-free base id with the canonical helper, exactly
    as the production PM-submit path does, for ``_open_command``'s NVDA ticker.
    """
    base = f"inv-X.{envelope_id}.0.0"
    return derive_pm_command_id(
        invocation_id="inv-X",
        envelope_id=envelope_id,
        command_ordinal=0,
        attempt_seq=0,
        thesis_id=derive_open_thesis_id(ticker, base),
    )


def _options_open() -> OpenCommand:
    """An options OPEN carrying the mandatory PnL-denominated capital floor (ALP-848).

    Single-leg long call with a LIMIT entry so the OPEN reserves capital, and a
    ``capital_protection_floor`` so the dispatcher submits the always-on
    broker-enforced floor alongside the entry (ALP-856).
    """
    return OpenCommand(
        command_type="open",
        instrument=OptionInstrument(
            asset_type="option",
            underlying="NVDA",
            strike=price(900.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
        ),
        entry_order=EntryOrder(type="limit", limit_price=price(12.0), stop_price=None),
        position_size=PositionSize(quantity=2.0, dollar_value=money(2_400.0)),
        target=Target(
            target_type="absolute_price",
            price=price(20.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                trigger_signal="underlying_price",
                condition=PriceCondition(
                    underlying_trigger="NVDA",
                    comparator="<=",
                    trigger_price=price(750.0),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary="Long NVDA call.",
            nature="directional",
            components=(
                OMSThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference="NVDA",
                    narrative="Capex tailwind.",
                    key_assumptions=("Capex stays elevated.",),
                ),
            ),
        ),
        capital_protection_floor=CapitalProtectionFloor(max_loss=money(800.0)),
    )


def _options_open_result(command_id: str) -> SubmissionResult:
    """An accepted ``SubmissionResult`` carrying the validation greeks an options
    OPEN writeback requires (``OptionsPositionDetails`` cannot build without
    ``validation_metadata.greeks`` + ``.implied_volatility``)."""
    return SubmissionResult(
        command_ordinal=0,
        status="accepted",
        command_id=command_id,
        acknowledgment=Acknowledgment(
            validation_metadata=_ValidationMetadata(
                greeks=Greeks(delta=0.5, gamma=0.02, theta=-0.04, vega=0.2),
                implied_volatility=0.30,
                delta_adjusted_exposure=0.0,
                per_rule_headroom=(),
            ),
        ),
    )


async def _read_order_by_order_id(
    factory: async_sessionmaker[AsyncSession], order_id: str
) -> OrderRow | None:
    async with factory() as sess:
        return await sess.get(OrderRow, order_id)


async def _read_floor_leg_order_id(
    factory: async_sessionmaker[AsyncSession], *, bracket_id: str
) -> str | None:
    """The ``order_id`` of the bracket's BROKER_ENFORCED capital-floor leg."""
    async with factory() as sess:
        rows = list(
            (
                await sess.execute(
                    select(BracketLegRow).where(BracketLegRow.bracket_id == bracket_id)
                )
            ).scalars()
        )
    floor = [r for r in rows if r.enforcement_binding == "broker_enforced"]
    assert len(floor) == 1, f"expected exactly one broker-enforced floor leg, got {len(floor)}"
    return floor[0].order_id


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
    # ALP-847 — a not-yet-routed order carries NO broker id (NULL), never a
    # synthetic placeholder; the real id is backfilled on dispatch.
    assert row.alpaca_order_id is None


@pytest.mark.asyncio
async def test_precommit_open_reserves_capital_and_marks_entry_pending_submit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_invocation_substrate(factory, invocation_id=_INV)
    await _seed_cash_ledger(factory)
    cmd = _limit_open()
    cid = _open_cid("ENV-SA-2")
    result = _accepted_result(0, cid)

    landed = await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    assert landed is True
    row = await _read_order_by_client_order_id(factory, cid)
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
    cid = _open_cid("ENV-SA-3")
    result = _accepted_result(0, cid)

    assert await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)
    # Replay — must be a no-op: one row, one reservation.
    assert await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)

    async with factory() as sess:
        rows = (
            (await sess.execute(select(OrderRow).where(OrderRow.client_order_id == cid)))
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
    cid = _open_cid("ENV-SA-5")
    result = _accepted_result(0, cid)
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

    entry = await _read_order_by_client_order_id(factory, cid)
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
# (A)+(C) options OPEN capital floor — durable floor OrderRow + FK-safe leg
# (ALP-856 / FS4): the floor is a tracked broker order with its own OrderRow,
# the floor bracket_legs.order_id points at that OrderRow (satisfying the FK),
# and the floor's broker id is backfilled onto OrderRow.alpaca_order_id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_open_atomic_path_persists_floor_orderrow_fk_safe(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-856 — an options OPEN through the ALP-836 atomic path persists the
    floor's OrderRow + the floor ``bracket_legs`` row through an FK-enforced DB
    with NO ``FOREIGN KEY constraint failed``, and the floor leg's ``order_id``
    resolves to an OrderRow whose ``alpaca_order_id`` is the backfilled broker id.

    The floor is precommitted PENDING_SUBMIT with ``alpaca_order_id`` NULL keyed
    by its deterministic floor ``client_order_id``; the broker id rides back on
    ``leg_alpaca_order_ids['capital_floor']`` and is backfilled onto the floor
    OrderRow (not onto the leg, which points at the OMS order_id).
    """
    _, factory = db
    await _seed_invocation_substrate(factory, invocation_id=_INV)
    await _seed_cash_ledger(factory)
    cmd = _options_open()
    cid = _open_cid("ENV-REC-9")
    result = _options_open_result(cid)

    # (A) pre-commit — durable graph, no broker ids; floor PENDING_SUBMIT.
    landed = await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)
    assert landed is True

    floor_cid = derive_capital_floor_client_order_id(cid)
    floor_order_id = _capital_floor_order_id(cid)
    floor_row = await _read_order_by_order_id(factory, floor_order_id)
    assert floor_row is not None, "the floor OrderRow must be precommitted"
    assert floor_row.status == OrderStatus.PENDING_SUBMIT.value
    assert floor_row.client_order_id == floor_cid
    assert floor_row.alpaca_order_id is None

    # The floor bracket_legs.order_id points at the floor OMS order_id (FK-safe).
    entry = await _read_order_by_client_order_id(factory, cid)
    assert entry is not None
    leg_order_id = await _read_floor_leg_order_id(factory, bracket_id=entry.bracket_id)
    assert leg_order_id == floor_order_id

    # (C) backfill — the floor's broker id lands on the floor OrderRow.
    await backfill_command_broker_ids(
        factory,
        command=cmd,
        result=result,
        dispatch_result=_dispatch_result(
            "entry-uuid",
            leg_alpaca_order_ids={"capital_floor": "floor-uuid"},
            payload_kind="options",
        ),
    )

    floor_row = await _read_order_by_order_id(factory, floor_order_id)
    assert floor_row is not None
    assert floor_row.status == OrderStatus.PENDING.value
    assert floor_row.alpaca_order_id == "floor-uuid"
    # The leg still points at the OMS order_id; resolving it → OrderRow →
    # alpaca id recovers the resting floor's real broker id (the closer path).
    resolved = await _read_order_by_order_id(factory, leg_order_id)
    assert resolved is not None
    assert resolved.alpaca_order_id == "floor-uuid"


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
    cid = _open_cid("ENV-SA-7")
    result = _accepted_result(0, cid)
    await precommit_command(factory, invocation_id=_INV, command=cmd, result=result)
    assert await _read_reserved_capital(factory) == pytest.approx(9000.0)

    await abandon_command(
        factory, invocation_id=_INV, command=cmd, result=result, reason="broker_gateway_failure"
    )

    entry = await _read_order_by_client_order_id(factory, cid)
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
