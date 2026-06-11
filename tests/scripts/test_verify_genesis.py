"""Genesis-verify script — the §8 first-run assertions made executable (ALP-858).

``scripts/verify/verify_genesis.py`` (module:
:mod:`alphamind.scripts.verify_genesis`) asserts the genesis-cutover runbook §8
checklist on a fresh account: zero reconciliation alerts (by construction), a
canary entry fill that self-attributes via the broker-carried link (no
``unattributed_fills`` row, no order-row dependency), projection (quantity) and
Intent (thesis / reservation / cost basis) both reflecting the canary, greeks in
the ``position_greeks`` side table, the options capital-floor ``stop_limit``
resting at the broker, and a fired monitor stop submitting a fresh
self-attributing close with no ``alp-…`` synthetic id anywhere.

The DB is the sanctioned mock boundary; here it is a real in-memory SQLite at
schema head. The canary is driven through the **production** primitives
(:func:`bootstrap_singletons_from_alpaca`, :func:`persist_fill_report`, the real
state codecs and ``command_ids`` derivation) so the assertions exercise the
post-wave behaviors, not re-implementations of them. No live account.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.scripts.verify_genesis import (
    GENESIS_CANARY,
    assert_canary_self_attributes,
    assert_greeks_in_side_table,
    assert_no_synthetic_broker_ids,
    assert_options_floor_resting,
    assert_projection_and_intent_reflect_canary,
    assert_zero_reconciliation_alerts,
    run_genesis_verification,
    seed_canary,
    seed_flat_genesis,
)


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite engine with the full ORM schema, fresh and empty."""
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    # Apply foreign_keys=ON the same way production does — the genesis canary's
    # deferred-FK cluster only commits cleanly under FK enforcement.
    with make_session_factory(sync_engine)():
        pass
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


class TestZeroReconciliationAlerts:
    async def test_flat_genesis_has_zero_reconciliation_alerts(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A flat-account bootstrap emits no RECONCILIATION_ALERT/CORRECTION rows.

        Genesis is clean by construction (ADR-0001 / invariant 6): nothing to
        reconcile, so the activity log carries no reconciliation events.
        """
        await seed_flat_genesis(session_factory)

        async with session_factory() as session:
            failure = await assert_zero_reconciliation_alerts(session)
        assert failure is None


class TestCanarySelfAttribution:
    async def test_canary_entry_fill_self_attributes_with_no_order_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The canary entry fill attributes via the broker-carried link to its
        thesis / invocation / position, appends one ``broker_event_log`` FILL row,
        and never reaches the ``unattributed_fills`` strand — with no ``orders``
        row resolving it (02a / invariant 2)."""
        from sqlalchemy import select as sa_select

        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )
        from alphamind.state.tables.broker_event_log import BrokerEventLogRow

        await seed_flat_genesis(session_factory)
        await seed_canary(session_factory)

        async with session_factory() as session:
            failure = await assert_canary_self_attributes(session)
            assert failure is None, failure

            # The canary entry fill is on the append-only event log, attributed.
            rows = (await session.execute(sa_select(BrokerEventLogRow))).scalars().all()
            fills = [r for r in rows if r.event_type == "FILL"]
            assert len(fills) >= 1
            entry = fills[0]
            assert entry.thesis_id == GENESIS_CANARY.thesis_id
            assert entry.invocation_id == GENESIS_CANARY.invocation_id
            assert entry.position_id == GENESIS_CANARY.position_id
            # No strand for an AlphaMind-submitted (linked) fill.
            assert await list_unattributed_fills(session) == []


class TestProjectionAndIntent:
    async def test_projection_and_intent_both_reflect_the_canary(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The projection ``positions`` row carries the canary quantity and the
        Intent (``thesis_pnl_ledger`` cost basis + ``capital_reservations``) the
        thesis / reservation / cost basis (03c / 04b)."""
        await seed_flat_genesis(session_factory)
        await seed_canary(session_factory)

        async with session_factory() as session:
            assert await assert_projection_and_intent_reflect_canary(session) is None


class TestGreeksSideTable:
    async def test_greeks_land_in_the_side_table(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The canary's greeks land in ``position_greeks`` keyed by position_id
        (the single-writer side table, ADR-0005 / 04b)."""
        await seed_flat_genesis(session_factory)
        await seed_canary(session_factory)

        async with session_factory() as session:
            assert await assert_greeks_in_side_table(session) is None


class TestOptionsFloorResting:
    async def test_options_capital_floor_stop_limit_rests(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The options capital floor is a durable resting ``STOP_LIMIT``
        ``OrderRow`` at the broker (04c / ADR-0003)."""
        await seed_flat_genesis(session_factory)
        await seed_canary(session_factory)

        async with session_factory() as session:
            assert await assert_options_floor_resting(session) is None


class TestNoSyntheticBrokerIds:
    async def test_no_alp_synthetic_id_anywhere(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """No ``alp-…`` synthetic broker id exists on any order — neither the
        entry, the resting floor, nor a fired monitor-stop close (invariant 5)."""
        await seed_flat_genesis(session_factory)
        await seed_canary(session_factory)

        async with session_factory() as session:
            assert await assert_no_synthetic_broker_ids(session) is None

    async def test_close_client_order_id_carries_no_alp_prefix(self) -> None:
        """The monitor-stop close's engine-originated id self-attributes (thesis +
        invocation) and carries no ``alp-`` synthetic placeholder."""
        close_cid = GENESIS_CANARY.close_client_order_id
        assert "alp-" not in close_cid
        assert GENESIS_CANARY.thesis_id in close_cid


class TestRunGenesisVerification:
    async def test_ephemeral_genesis_passes_all_checks(self) -> None:
        """The self-contained ephemeral run (no ``--db-path``) seeds a flat genesis
        + the canary and passes every §8 assertion."""
        assert await run_genesis_verification(db_path=None) is True
