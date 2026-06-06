"""Tests for ``alphamind.scheduler.control.adapters`` production verb adapters.

``ActivityLogEmergencyTrigger.trigger()`` is the operator-console emergency
path (``POST /control/trigger_emergency_invocation``). These tests drive it
against a real in-memory SQLite session — no fake substituted for the ORM
write — which is the exact seam the original ``TypeError`` (wrong ORM field
``timestamp``) and ``IntegrityError`` (NOT-NULL ``invocation_id=None``) hid
behind. The verb/route tests fake the ``EmergencyTrigger`` Protocol, so this
file is the only coverage of the live activity-log write path (ALP-869).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.portfolio_state.events.activity_log import (
    EmergencyInvocationRequestedDetail,
    decode_detail,
)
from alphamind.scheduler.control.adapters import ActivityLogEmergencyTrigger
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    invocation_record_to_row,
)
from alphamind.state.tables.activity_log import ActivityLogRow

_PROCESS_LIFETIME_ID = "proc-driver-1"


def _make_invocation_record(*, invocation_id: str, start_at: str) -> InvocationRecord:
    """Build a minimal scheduled-invocation record (FK target for activity_log)."""
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        start_at=start_at,
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="test",
        trigger_reason="seed",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=f"/tmp/provenance/invocations/{invocation_id}/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/data_calibration.json"
        ),
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def _seed_invocations(
    factory: async_sessionmaker[AsyncSession],
    *,
    records: list[InvocationRecord],
) -> None:
    async with factory() as session:
        for record in records:
            session.add(invocation_record_to_row(record))
        await session.commit()


class TestTriggerPersistsRow:
    async def test_trigger_writes_one_row_bound_to_latest_invocation(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``trigger`` persists exactly one activity-log row mirroring the
        monitor's emergency-emit shape, bound to the most-recently-started
        invocation, and returns a ``pending-emerg-opcon-inv-…`` handle."""
        # Two invocations with distinct start_at — the row must bind to the LATER.
        await _seed_invocations(
            async_factory,
            records=[
                _make_invocation_record(invocation_id="inv-older", start_at="2026-05-07T13:00:00Z"),
                _make_invocation_record(
                    invocation_id="inv-latest", start_at="2026-05-07T14:30:00Z"
                ),
            ],
        )
        trigger = ActivityLogEmergencyTrigger(session_factory=async_factory, cooldown_minutes=30)
        now = datetime(2026, 5, 7, 15, 0, 0, tzinfo=UTC)

        handle = await trigger.trigger(
            reason="manual operator escalation", source="operator_console", now=now
        )

        assert handle.startswith("pending-emerg-opcon-inv-")

        async with async_factory() as session:
            rows = (await session.execute(select(ActivityLogRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "EMERGENCY_INVOCATION_REQUESTED"
        assert row.event_group == "RISK_AND_GUARDRAIL"
        assert row.source == "GUARDRAIL_LAYER"
        assert row.entry_at is not None
        assert row.invocation_id == "inv-latest"

        detail = decode_detail(row.detail_json, EmergencyInvocationRequestedDetail)
        assert detail.trigger_type == "multi_rule_breach"
        assert detail.trigger_reason == "operator_console:manual operator escalation"

    async def test_trigger_raises_when_no_invocations_exist(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """With no ``invocations`` row to satisfy the NOT-NULL FK, ``trigger``
        raises rather than writing an FK-violating row."""
        trigger = ActivityLogEmergencyTrigger(session_factory=async_factory, cooldown_minutes=30)
        now = datetime(2026, 5, 7, 15, 0, 0, tzinfo=UTC)

        with pytest.raises(RuntimeError):
            await trigger.trigger(
                reason="manual operator escalation",
                source="operator_console",
                now=now,
            )

        async with async_factory() as session:
            rows = (await session.execute(select(ActivityLogRow))).scalars().all()
        assert rows == []
