"""Tests for the corporate-action integration-ledger table (story 05 / ALP-363).

The table tracks which Alpaca CA activities have been integrated into state.
One row per activity, keyed on ``alpaca_activity_id``. Idempotent on Phase 1
retry — re-marking the same activity is a no-op rather than an error.

Covers:
* Column shape, indexes, FK on ``processing_invocation_id``, CHECK on
  ``processing_status``.
* ``mark_ca_activity_processed`` inserts inside the open ``InvocationContext``
  transaction; idempotent on the PK.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import Session

from alphamind.execution.write_paths.ca_integration_ledger import (
    mark_ca_activity_processed,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.invocation_context.context import (
    InvocationContext,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)

INTEGRATED_AT = datetime(2026, 5, 7, 13, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _process_lifetime() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/p/pip.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux",
    )


def _invocation_record(invocation_id: str = "inv-1") -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at="2026-05-07T14:30:00Z",
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/p/cfg.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/p/cal.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import alphamind.state.tables  # noqa: F401

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


@pytest.fixture()
async def async_engine_and_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime()))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestCorporateActionLedgerTableShape:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("corporate_action_integration_ledger")}
        assert cols == {
            "alpaca_activity_id",
            "processing_invocation_id",
            "processing_timestamp",
            "processing_status",
        }
        pk = insp.get_pk_constraint("corporate_action_integration_ledger")
        assert pk["constrained_columns"] == ["alpaca_activity_id"]

    def test_indexes_present(self, engine: Engine) -> None:
        insp = inspect(engine)
        names = {idx["name"] for idx in insp.get_indexes("corporate_action_integration_ledger")}
        assert "ix_ca_ledger_processing_invocation_id" in names
        assert "ix_ca_ledger_processing_status" in names

    def test_foreign_key_to_invocations(self, engine: Engine) -> None:
        insp = inspect(engine)
        fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in insp.get_foreign_keys("corporate_action_integration_ledger")
        }
        assert (("processing_invocation_id",), "invocations") in fks


# ---------------------------------------------------------------------------
# mark_ca_activity_processed inside InvocationContext
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMarkCaActivityProcessed:
    async def test_inserts_one_row_inside_invocation_context(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        ctx = InvocationContext(session_factory=factory, record=_invocation_record())
        async with ctx as handle:
            await mark_ca_activity_processed(
                handle, "alpaca-act-1", processing_timestamp=INTEGRATED_AT
            )

        async with factory() as session:
            rows = (
                (await session.execute(select(CorporateActionIntegrationLedgerRow))).scalars().all()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row.alpaca_activity_id == "alpaca-act-1"
        assert row.processing_invocation_id == "inv-1"
        assert row.processing_status == "processed"

    async def test_idempotent_on_alpaca_activity_id(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        ctx1 = InvocationContext(session_factory=factory, record=_invocation_record("inv-1"))
        async with ctx1 as handle:
            await mark_ca_activity_processed(
                handle, "alpaca-act-1", processing_timestamp=INTEGRATED_AT
            )
            await mark_ca_activity_processed(
                handle, "alpaca-act-1", processing_timestamp=INTEGRATED_AT
            )

        async with factory() as session:
            rows = (
                (await session.execute(select(CorporateActionIntegrationLedgerRow))).scalars().all()
            )
        assert len(rows) == 1

    async def test_idempotent_across_separate_invocations(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Phase 1 retry on a new invocation must not double-mark."""
        _, factory = async_engine_and_factory
        ctx1 = InvocationContext(session_factory=factory, record=_invocation_record("inv-1"))
        async with ctx1 as handle:
            await mark_ca_activity_processed(
                handle, "alpaca-act-1", processing_timestamp=INTEGRATED_AT
            )

        ctx2 = InvocationContext(session_factory=factory, record=_invocation_record("inv-2"))
        async with ctx2 as handle:
            await mark_ca_activity_processed(
                handle, "alpaca-act-1", processing_timestamp=INTEGRATED_AT
            )

        async with factory() as session:
            rows = (
                (await session.execute(select(CorporateActionIntegrationLedgerRow))).scalars().all()
            )
        assert len(rows) == 1
        # First-write-wins: the original invocation_id is preserved.
        assert rows[0].processing_invocation_id == "inv-1"
