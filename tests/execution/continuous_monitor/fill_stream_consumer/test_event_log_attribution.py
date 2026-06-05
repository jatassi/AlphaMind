"""Self-attributing fill → broker-event log (story 02a / ALP-845).

A fill's thesis/position attribution rides the broker-carried link embedded in
``client_order_id`` (ALP-844 / ADR-0002), so the fill appends to the append-only
``broker_event_log`` with the decoded ``thesis_id`` / ``invocation_id`` /
``position_id`` and **no local ``orders`` row is required**. The strand path
(``unattributed_fills``) is reachable only for a genuinely out-of-band fill that
carries no parseable link.

These tests exercise :func:`persist_fill_report` directly against an on-disk
SQLite schema (the DB is one of the four sanctioned mock boundaries; here it is
real). The link is built through the canonical
:func:`derive_pm_command_id` / :func:`derive_engine_command_id` so the fixtures
never hand-write the id grammar.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alpaca.trading.enums import (
    AssetClass,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.models import Order, TradeUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    BracketId,
    InvocationId,
    OrderId,
    PositionId,
    ThesisId,
)
from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    persist_fill_report,
)
from alphamind.execution.oms.command_ids import (
    derive_engine_command_id,
    derive_open_thesis_id,
    derive_pm_command_id,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.orders import OrderRow
from tests.state._fk_substrate import (
    seed_position_cluster,
    stub_invocation_row,
    stub_order_row,
    stub_process_lifetime_row,
)

# ---------------------------------------------------------------------------
# A linked command id built through the canonical derivation (no hand-writing).
# ---------------------------------------------------------------------------

_INVOCATION_ID = "20260603T120000Z-abc123"
_BASE_COMMAND_ID = f"inv-{_INVOCATION_ID}.ENV-SA-1.0.0"
_THESIS_ID = derive_open_thesis_id("AAPL", _BASE_COMMAND_ID)
_PM_LINKED_COMMAND_ID = derive_pm_command_id(
    invocation_id=_INVOCATION_ID,
    envelope_id="ENV-SA-1",
    command_ordinal=0,
    attempt_seq=0,
    thesis_id=_THESIS_ID,
)
_ENGINE_LINKED_COMMAND_ID = derive_engine_command_id(
    monitor_session_id="mon-20260511T120000Z-deadbeef",
    trigger_id=1,
    thesis_id=_THESIS_ID,
    invocation_id=_INVOCATION_ID,
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _build_order(
    *,
    client_order_id: str,
    order_id: UUID | None = None,
    qty: str = "1",
    filled_qty: str = "1",
    status: AlpacaOrderStatus = AlpacaOrderStatus.FILLED,
) -> Order:
    return Order(
        id=order_id or uuid4(),
        client_order_id=client_order_id,
        created_at=_now_utc(),
        updated_at=_now_utc(),
        submitted_at=_now_utc(),
        symbol="AAPL",
        asset_class=AssetClass.US_EQUITY,
        order_class=OrderClass.SIMPLE,
        order_type=OrderType.LIMIT,
        type=OrderType.LIMIT,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        status=status,
        extended_hours=False,
        qty=qty,
        filled_qty=filled_qty,
    )


def _fill_report(
    *,
    client_order_id: str,
    order_id: UUID | None = None,
    price: float | None = 189.42,
    qty: float | None = 1.0,
    event: str = "fill",
    filled_qty: str = "1",
    timestamp: datetime | None = None,
) -> FillReport:
    reports = translate_trade_update(
        TradeUpdate(
            event=event,
            order=_build_order(
                client_order_id=client_order_id, order_id=order_id, filled_qty=filled_qty
            ),
            timestamp=timestamp or _now_utc(),
            price=price,
            qty=qty,
        )
    )
    assert len(reports) == 1
    return reports[0]


_FIXED_TS = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite engine with the full ORM schema.

    Seeds the thesis/position/invocation FK targets the broker-carried link
    points at, but deliberately leaves the ``orders`` row absent in the
    no-order-row tests (the entry-order in the cluster is reused as the link's
    position/thesis edge, NOT as the fill's resolution hop).
    """
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        # Seed the position/thesis/bracket cluster keyed on the LINK's thesis id
        # and a position the thesis points at, plus the invocation FK target.
        seed_position_cluster(
            sess,
            position_id=PositionId("pos-1"),
            thesis_id=ThesisId(_THESIS_ID),
            bracket_id=BracketId("bracket-1"),
            entry_order_id=OrderId("order-cluster-1"),
        )
        sess.add(stub_process_lifetime_row())
        sess.flush()
        sess.add(stub_invocation_row(InvocationId(f"inv-{_INVOCATION_ID}")))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


async def _read_event_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[BrokerEventLogRow]:
    async with session_factory() as session:
        result = await session.execute(select(BrokerEventLogRow))
        return list(result.scalars().all())


def _logged_fill_quantity_total(rows: list[BrokerEventLogRow]) -> float:
    """Sum ``fill_quantity`` over the FILL rows — the qty the 03c fold sees."""
    return sum(
        float(json.loads(r.raw_payload_json)["fill_quantity"])
        for r in rows
        if r.event_type == "FILL"
    )


# ---------------------------------------------------------------------------
# AC 1 — self-attribute via the link, no order row, append to the event log
# ---------------------------------------------------------------------------


class TestSelfAttribution:
    async def test_pm_linked_fill_appends_event_log_row_with_no_order_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A PM-originated fill whose ``client_order_id`` carries the link
        attributes to its thesis / invocation / position and appends a
        ``broker_event_log`` row — with no ``orders`` row resolving it, and
        without ever reaching the ``unattributed_fills`` strand path (AC 1 + 4)."""
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )

        report = _fill_report(client_order_id=_PM_LINKED_COMMAND_ID, order_id=uuid4())

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1
        (row,) = rows
        assert row.event_type == "FILL"
        assert row.thesis_id == _THESIS_ID
        assert row.invocation_id == f"inv-{_INVOCATION_ID}"
        assert row.position_id == "pos-1"
        # The strand path is unreachable for an AlphaMind-submitted (linked) fill.
        async with session_factory() as session:
            assert await list_unattributed_fills(session) == []

    async def test_engine_linked_fill_self_attributes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """An engine-originated close fill (``MON.…~the-…~inv-…``) self-attributes
        through the same link parse — exercising the engine form, not just PM."""
        report = _fill_report(client_order_id=_ENGINE_LINKED_COMMAND_ID, order_id=uuid4())

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1
        (row,) = rows
        assert row.thesis_id == _THESIS_ID
        assert row.invocation_id == f"inv-{_INVOCATION_ID}"


class TestNativeLegStrandHole:
    async def test_projection_cache_with_unresolved_link_is_quarantined_not_null_fk(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A native-bracket child fill that resolves an order row whose
        position→thesis link is not yet committed (both thesis_id and
        position_id None) must be QUARANTINED — never land a NULL-FK
        ``broker_event_log`` row (append-only, never enriched). The drain
        retries it once the link commits (B1)."""
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )

        # An order row that resolves by broker UUID but carries NO position link
        # yet (position_id NULL) — the native-bracket child whose parent OPEN
        # writeback has not committed the position→thesis edge.
        broker_uuid = uuid4()
        async with session_factory() as session:
            session.add(
                stub_order_row(
                    "order-native-child",
                    "bracket-1",
                    position_id=None,
                    alpaca_order_id=str(broker_uuid),
                )
            )
            await session.commit()

        # The native-bracket child carries an Alpaca-generated (link-less)
        # client_order_id, so it takes the projection-cache path.
        report = _fill_report(client_order_id="alpaca-generated-native", order_id=broker_uuid)

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        # No NULL-FK event-log row landed.
        assert await _read_event_log(session_factory) == []
        # It was quarantined for the drain to retry.
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1


class TestUncommittedThesisStrandHole:
    async def test_linked_fill_whose_thesis_is_uncommitted_is_quarantined_not_stranded(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A linked fill whose ``client_order_id`` names a thesis row that has not
        committed yet (genesis OPEN fast-fill race) must be QUARANTINED — not
        appended with a deferred ``thesis_id`` FK that violates at ``commit()``.

        ``broker_event_log.thesis_id`` is DEFERRABLE INITIALLY DEFERRED, so an
        append referencing a not-yet-committed thesis passes the INSERT but
        raises at commit, stranding the fill (neither logged nor quarantined).
        The drain retries once the thesis lands (FS5 / B1)."""
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )

        # A linked PM command id whose thesis id is NOT one the fixture seeded —
        # the OPEN's Phase-2 commit has not landed the ``theses`` row yet.
        uncommitted_thesis_id = derive_open_thesis_id("MSFT", _BASE_COMMAND_ID)
        assert uncommitted_thesis_id != _THESIS_ID
        unlanded_command_id = derive_pm_command_id(
            invocation_id=_INVOCATION_ID,
            envelope_id="ENV-SA-1",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=uncommitted_thesis_id,
        )
        report = _fill_report(client_order_id=unlanded_command_id, order_id=uuid4())

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        # No deferred-FK-violating event-log row landed — and the commit did not raise.
        assert await _read_event_log(session_factory) == []
        # It was quarantined for the drain to retry once the thesis commits.
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1


class TestBootstrapSentinelInvocation:
    async def test_close_fill_with_bootstrap_sentinel_invocation_persists_null_invocation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A self-attributing close fill whose engine link carries the cold-start
        ``monitor-bootstrap`` sentinel invocation persists with ``invocation_id``
        NULL — not the non-existent ``inv-monitor-bootstrap`` FK that would
        violate the deferred ``invocations`` FK at commit and strand the fill.

        On a fresh DB with no invocations the monitor's provider returns the
        ``monitor-bootstrap`` sentinel; the closer weaves it into the engine
        ``client_order_id`` and the returning fill parses it back as
        ``InvocationId("inv-monitor-bootstrap")``. The column is nullable and the
        thesis link still attributes the fill, so the sentinel (no matching
        ``invocations`` row) is stored as NULL (CL3)."""
        bootstrap_command_id = derive_engine_command_id(
            monitor_session_id="mon-20260511T120000Z-deadbeef",
            trigger_id=2,
            thesis_id=_THESIS_ID,
            invocation_id="monitor-bootstrap",
        )
        report = _fill_report(client_order_id=bootstrap_command_id, order_id=uuid4())

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1
        (row,) = rows
        assert row.event_type == "FILL"
        assert row.thesis_id == _THESIS_ID
        # The bootstrap sentinel invocation has no ``invocations`` row -> stored NULL,
        # not the non-existent FK that would have raised at commit.
        assert row.invocation_id is None
        assert row.position_id == "pos-1"


class TestEventLogIdempotency:
    async def test_same_fill_delivered_twice_collapses_to_one_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The same fill delivered twice (websocket + recovery replay) collapses
        to one ``broker_event_log`` row, keyed on the ``event_key`` PK."""
        order_uuid = uuid4()
        first = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID, order_id=order_uuid, timestamp=_FIXED_TS
        )
        # A re-delivered copy of the SAME broker fact — same order UUID, price,
        # qty, timestamp; its derived event_key is identical, so the insert
        # collapses onto the existing row.
        replay = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID, order_id=order_uuid, timestamp=_FIXED_TS
        )

        await persist_fill_report(first, session_factory=session_factory, enrichment_callable=None)
        await persist_fill_report(replay, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1


class TestTerminalStatusEvent:
    """ALP-849 / W1c — a zero-fill terminal order-status event appends a
    ``TERMINAL_ORDER_STATUS`` row to the append-only log instead of RMW-ing
    ``orders.status``. The continuous monitor is no longer a second writer on
    the shared ``orders`` row (invariant 1)."""

    async def test_zero_fill_cancel_appends_terminal_event_carrying_the_link(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A ``canceled`` event for a linked entry (its ``client_order_id`` is
        the PM command id) appends one ``TERMINAL_ORDER_STATUS`` event-log row
        carrying the decoded thesis / invocation / position link."""
        report = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=uuid4(),
            qty=1.0,
            event="canceled",
            filled_qty="0",
            price=None,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1
        (row,) = rows
        assert row.event_type == "TERMINAL_ORDER_STATUS"
        assert row.thesis_id == _THESIS_ID
        assert row.invocation_id == f"inv-{_INVOCATION_ID}"
        assert row.position_id == "pos-1"

    async def test_monitor_appends_event_and_does_not_rmw_orders_status(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Invariant 1: the monitor's terminal path appends an event and leaves
        ``orders.status`` UNTOUCHED. The status is now a projection derived from
        the log by the single (pipeline) writer — the monitor is no longer a
        second writer on the shared ``orders`` row."""
        # A durable PENDING entry row carrying the command id as client_order_id
        # (the ALP-836 pre-backfill resolution path); the terminal event resolves
        # to it but must NOT write its status.
        async with session_factory() as session:
            session.add(
                stub_order_row(
                    "order-entry-pending",
                    "bracket-1",
                    position_id="pos-1",
                    status="PENDING",
                    client_order_id=_PM_LINKED_COMMAND_ID,
                )
            )
            await session.commit()

        report = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=uuid4(),
            event="canceled",
            filled_qty="0",
            price=None,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        # The terminal fact is captured on the append-only log.
        rows = await _read_event_log(session_factory)
        assert [r.event_type for r in rows] == ["TERMINAL_ORDER_STATUS"]
        # And the shared orders row was never RMW'd by the monitor.
        async with session_factory() as session:
            order = await session.get(OrderRow, "order-entry-pending")
            assert order is not None
            assert order.status == "PENDING"

    async def test_oco_sibling_cancel_attributes_via_order_edge_no_invocation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """An OCO sibling-cancel (Alpaca-generated, link-less ``client_order_id``,
        resolved by captured broker UUID) appends a terminal event carrying the
        thesis / position off the order's position edge — invocation ``NULL``
        (no link carries the *when*)."""
        sl_uuid = uuid4()
        async with session_factory() as session:
            session.add(
                stub_order_row(
                    "order-stop-leg",
                    "bracket-1",
                    position_id="pos-1",
                    role="PRICE_STOP",
                    direction="SELL",
                    status="PENDING",
                    alpaca_order_id=str(sl_uuid),
                )
            )
            await session.commit()

        report = _fill_report(
            client_order_id="alpaca-generated-child-sl",
            order_id=sl_uuid,
            event="canceled",
            filled_qty="0",
            price=None,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1
        (row,) = rows
        assert row.event_type == "TERMINAL_ORDER_STATUS"
        assert row.thesis_id == _THESIS_ID
        assert row.position_id == "pos-1"
        assert row.invocation_id is None
        # No status RMW on the resolved leg row.
        async with session_factory() as session:
            leg = await session.get(OrderRow, "order-stop-leg")
            assert leg is not None
            assert leg.status == "PENDING"

    async def test_partial_fill_then_terminal_is_gated_out(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A partially-filled-then-terminal order appends NO terminal event — the
        fill path + Phase 1 own ``filled_quantity`` and integrate the partials
        (the ALP-739 zero-fill scoping, preserved)."""
        report = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=uuid4(),
            qty=10.0,
            event="expired",
            filled_qty="3",
            price=None,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        assert await _read_event_log(session_factory) == []

    async def test_none_cumulative_terminal_is_not_treated_as_zero_fill(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A canceled event whose broker ``filled_qty`` is ``None`` (broker
        reported nothing, not 0) must NOT append a zero-fill terminal event.

        A partially-filled-then-canceled order can arrive with a ``None``
        cumulative; defaulting that to ``0.0`` would let it pass the zero-fill
        terminal guard and mis-project a no-fill terminal over an order that had
        partials. ``None`` is unknown-not-zero, so the fill path + Phase 1 own
        it and this path skips (FS3)."""
        unknown_cumulative = TradeUpdate(
            event="canceled",
            order=Order(
                id=uuid4(),
                client_order_id=_PM_LINKED_COMMAND_ID,
                created_at=_now_utc(),
                updated_at=_now_utc(),
                submitted_at=_now_utc(),
                symbol="AAPL",
                asset_class=AssetClass.US_EQUITY,
                order_class=OrderClass.SIMPLE,
                order_type=OrderType.LIMIT,
                type=OrderType.LIMIT,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                status=AlpacaOrderStatus.CANCELED,
                extended_hours=False,
                qty="10",
                filled_qty=None,
            ),
            timestamp=_now_utc(),
            price=None,
            qty=None,
        )
        (report,) = translate_trade_update(unknown_cumulative)
        assert report.cumulative_filled_quantity is None

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        assert await _read_event_log(session_factory) == []

    async def test_redelivered_terminal_event_collapses_to_one_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The same terminal disposition delivered twice (websocket + recovery
        replay) collapses to one ``broker_event_log`` row, keyed on the
        ``event_key`` PK (idempotency)."""
        order_uuid = uuid4()
        first = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="canceled",
            filled_qty="0",
            price=None,
            timestamp=_FIXED_TS,
        )
        replay = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="canceled",
            filled_qty="0",
            price=None,
            timestamp=_FIXED_TS,
        )

        await persist_fill_report(first, session_factory=session_factory, enrichment_callable=None)
        await persist_fill_report(replay, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1


class TestOutOfBandQuarantine:
    async def test_unlinked_fill_quarantined_and_not_in_event_log(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A fill whose ``client_order_id`` carries no parseable link (an
        out-of-band / manually-placed order) is parked on ``unattributed_fills``
        and alerted once — and never reaches ``broker_event_log``."""
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )

        report = _fill_report(client_order_id="totally-out-of-band", order_id=uuid4())

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        assert await _read_event_log(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].alerted is True

    async def test_redelivered_out_of_band_fill_does_not_realert(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A re-delivered copy of the same out-of-band fill collapses onto the
        existing queue row (idempotent on ``broker_fill_key``) and does NOT
        re-fire the one-time alert."""
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )

        order_uuid = uuid4()
        report = _fill_report(
            client_order_id="totally-out-of-band", order_id=order_uuid, timestamp=_FIXED_TS
        )
        replay = _fill_report(
            client_order_id="totally-out-of-band", order_id=order_uuid, timestamp=_FIXED_TS
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)
        await persist_fill_report(replay, session_factory=session_factory, enrichment_callable=None)

        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        # One row, alerted once — the re-park did not re-insert or re-alert.
        assert len(queued) == 1
        assert queued[0].retry_count == 0


class TestRecoveryGapReconciliation:
    """FS1 — a REST recovery report carries the CUMULATIVE ``filled_qty`` for an
    order, while the live websocket logs PER-EVENT partial increments. Appending
    the cumulative as-is would log the same shares twice (once as the partials,
    once as the cumulative) → the 03c PnL fold double-counts. The recovery path
    instead appends only the RESIDUAL GAP so ``broker_event_log`` reflects the
    order's true total EXACTLY ONCE."""

    async def test_recovery_after_partials_appends_only_the_residual_gap(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A websocket partial (60 of 100) is logged, then REST recovery sees the
        cumulative (100). Recovery must append ONE residual 40-share FILL — not a
        full 100-share duplicate — so the log totals 100 exactly once."""
        order_uuid = uuid4()
        # Live websocket partial: 60 shares @ p1, logged per-event.
        partial = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="partial_fill",
            price=189.42,
            qty=60.0,
            filled_qty="60",
            timestamp=_FIXED_TS,
        )
        await persist_fill_report(
            partial, session_factory=session_factory, enrichment_callable=None
        )

        # REST recovery sees the order's CUMULATIVE state: 100 filled @ avg price,
        # carried as one report whose ``fill_quantity`` == ``cumulative``.
        recovered = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="fill",
            price=190.00,
            qty=100.0,
            filled_qty="100",
        )
        assert recovered.fill_quantity == recovered.cumulative_filled_quantity == 100.0

        await persist_fill_report(
            recovered, session_factory=session_factory, enrichment_callable=None, recovered=True
        )

        rows = await _read_event_log(session_factory)
        # The log reflects the order's true total of 100 shares EXACTLY ONCE:
        # the 60-share partial + a single 40-share residual, NOT a 100-share dup.
        assert _logged_fill_quantity_total(rows) == 100.0
        residuals = [
            r
            for r in rows
            if r.event_type == "FILL"
            and float(json.loads(r.raw_payload_json)["fill_quantity"]) == 40.0
        ]
        assert len(residuals) == 1, "expected exactly one 40-share residual FILL"

    async def test_recovery_after_all_partials_logged_appends_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """When the websocket already logged the full quantity (two partials
        summing to the cumulative), a REST recovery of the same order sees gap == 0
        and appends NOTHING — the log stays complete, no duplicate row."""
        order_uuid = uuid4()
        for second, (qty, filled, price, event) in enumerate(
            ((40.0, "40", 189.00, "partial_fill"), (60.0, "100", 189.50, "fill"))
        ):
            partial = _fill_report(
                client_order_id=_PM_LINKED_COMMAND_ID,
                order_id=order_uuid,
                event=event,
                price=price,
                qty=qty,
                filled_qty=filled,
                timestamp=datetime(2026, 6, 3, 14, 30, second, tzinfo=UTC),
            )
            await persist_fill_report(
                partial, session_factory=session_factory, enrichment_callable=None
            )

        before = await _read_event_log(session_factory)
        assert _logged_fill_quantity_total(before) == 100.0

        recovered = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="fill",
            price=189.30,
            qty=100.0,
            filled_qty="100",
        )
        await persist_fill_report(
            recovered, session_factory=session_factory, enrichment_callable=None, recovered=True
        )

        after = await _read_event_log(session_factory)
        # Gap == 0 → no residual appended; the FILL rows are unchanged.
        assert len(after) == len(before)
        assert _logged_fill_quantity_total(after) == 100.0

    async def test_recovery_of_a_genuinely_dropped_fill_appends_the_full_quantity(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A fill the websocket NEVER logged (0 logged, cumulative 100) recovers
        the full 100 shares — the gap equals the whole cumulative when nothing is
        on the log yet, so the dropped fill is captured in full exactly once."""
        order_uuid = uuid4()
        recovered = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="fill",
            price=190.00,
            qty=100.0,
            filled_qty="100",
        )
        await persist_fill_report(
            recovered, session_factory=session_factory, enrichment_callable=None, recovered=True
        )

        rows = await _read_event_log(session_factory)
        assert _logged_fill_quantity_total(rows) == 100.0
        fills = [r for r in rows if r.event_type == "FILL"]
        assert len(fills) == 1

    async def test_recovered_fill_processed_twice_nets_to_one_total(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Idempotency: the SAME recovery snapshot processed twice nets to the
        same logged total — the second pass sees gap == 0 and appends nothing."""
        order_uuid = uuid4()
        recovered = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="fill",
            price=190.00,
            qty=100.0,
            filled_qty="100",
            timestamp=_FIXED_TS,
        )
        for _ in range(2):
            await persist_fill_report(
                recovered, session_factory=session_factory, enrichment_callable=None, recovered=True
            )

        rows = await _read_event_log(session_factory)
        assert _logged_fill_quantity_total(rows) == 100.0
        assert len([r for r in rows if r.event_type == "FILL"]) == 1

    async def test_live_websocket_fill_is_never_gap_reconciled(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The live websocket path (``recovered=False``) keeps appending per-event
        partials as-is — gap reconciliation is recovery-only. Two distinct live
        partials both land in full; neither is reduced against the other."""
        order_uuid = uuid4()
        first = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="partial_fill",
            price=189.00,
            qty=60.0,
            filled_qty="60",
            timestamp=datetime(2026, 6, 3, 14, 30, 1, tzinfo=UTC),
        )
        second = _fill_report(
            client_order_id=_PM_LINKED_COMMAND_ID,
            order_id=order_uuid,
            event="fill",
            price=189.50,
            qty=40.0,
            filled_qty="100",
            timestamp=datetime(2026, 6, 3, 14, 30, 2, tzinfo=UTC),
        )
        await persist_fill_report(first, session_factory=session_factory, enrichment_callable=None)
        await persist_fill_report(second, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_event_log(session_factory)
        # Both per-event increments logged in full (60 + 40), no reconciliation.
        assert _logged_fill_quantity_total(rows) == 100.0
        assert len([r for r in rows if r.event_type == "FILL"]) == 2
