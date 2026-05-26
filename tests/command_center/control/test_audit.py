"""Tests for ``command_center.control.audit`` (story 04a / ALP-668)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.control import (
    ControlErrorCode,
    ControlResult,
    ControlVerb,
)
from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center._kernel.operator_invocation import (
    operator_invocation,
)
from alphamind.command_center.control.audit import write_operator_action_entry
from alphamind.portfolio_state.events.activity_log import EventType
from alphamind.portfolio_state.events.types import EventSource
from alphamind.state.tables.activity_log import ActivityLogRow

from .conftest import PROCESS_LIFETIME_ID

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


async def _run_with_handle(
    factory: async_sessionmaker[AsyncSession],
    verb: ControlVerb,
    *,
    parameters: dict[str, object],
    result: ControlResult,
) -> str:
    """Open the operator-invocation handle, call audit, commit; return invocation id."""
    async with operator_invocation(
        production_session_factory=factory,
        process_lifetime_id=PROCESS_LIFETIME_ID,
        operator_session_id_=operator_session_id("sess-1"),
        verb=verb,
    ) as handle:
        entry = write_operator_action_entry(
            handle=handle,
            verb=verb,
            parameters=parameters,
            result=result,
            now=_NOW,
        )
        assert entry is not None or verb.value == "run_universe_validation"
        return handle.invocation_id


async def _read_rows_for(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> list[ActivityLogRow]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


class TestAuditWriterWritesRow:
    async def test_pause_writes_risk_parameter_changed_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        invocation_id = await _run_with_handle(
            production_session_factory,
            ControlVerb.PAUSE,
            parameters={"reason": "drawdown"},
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
        )
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == EventType.RISK_PARAMETER_CHANGED.value
        assert row.source == EventSource.OPERATOR_CONSOLE.value
        assert row.invocation_id == invocation_id
        assert "drawdown" in row.detail_json
        assert "operator_console_pause" in row.detail_json

    async def test_resume_writes_risk_parameter_changed_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        invocation_id = await _run_with_handle(
            production_session_factory,
            ControlVerb.RESUME,
            parameters={},
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
        )
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        assert "operator_console_resume" in rows[0].detail_json

    async def test_set_halt_mode_writes_row_with_enabled_field(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        invocation_id = await _run_with_handle(
            production_session_factory,
            ControlVerb.SET_HALT_MODE,
            parameters={"enabled": True, "reason": "vol spike"},
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
        )
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        assert "halt_mode_enabled" in rows[0].detail_json
        assert "vol spike" in rows[0].detail_json

    async def test_trigger_emergency_writes_emergency_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        invocation_id = await _run_with_handle(
            production_session_factory,
            ControlVerb.TRIGGER_EMERGENCY_INVOCATION,
            parameters={"reason": "ops triage"},
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
        )
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == EventType.EMERGENCY_INVOCATION_REQUESTED.value
        assert "operator_console" in row.detail_json
        assert "ops triage" in row.detail_json

    async def test_cancel_order_writes_order_cancelled_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        invocation_id = await _run_with_handle(
            production_session_factory,
            ControlVerb.CANCEL_ORDER,
            parameters={"order_id": "ord-99"},
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
        )
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == EventType.ORDER_CANCELLED.value
        assert row.order_id == "ord-99"
        assert "operator_cancel" in row.detail_json

    async def test_force_close_writes_row_with_envelope_id(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        invocation_id = await _run_with_handle(
            production_session_factory,
            ControlVerb.FORCE_CLOSE_POSITION,
            parameters={
                "position_id": "pos-42",
                "rationale": "wind down",
                "envelope_id": "MON.s1.7",
            },
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
        )
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.position_id == "pos-42"
        assert "MON.s1.7" in row.detail_json
        assert "force_close_requested" in row.detail_json


class TestAuditWriterSuppressionAndErrors:
    async def test_switch_profile_success_raises_value_error(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # SWITCH_PROFILE successes still must route through
        # emit_profile_switch_entry; F8 only widens the failure path.
        async with operator_invocation(
            production_session_factory=production_session_factory,
            process_lifetime_id=PROCESS_LIFETIME_ID,
            operator_session_id_=operator_session_id("sess-1"),
            verb=ControlVerb.SWITCH_PROFILE,
        ) as handle:
            with pytest.raises(ValueError, match="emit_profile_switch_entry"):
                write_operator_action_entry(
                    handle=handle,
                    verb=ControlVerb.SWITCH_PROFILE,
                    parameters={"profile_name": "large"},
                    result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
                    now=_NOW,
                )

    async def test_switch_profile_failure_writes_failure_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # F8 — failure path now writes a generic RISK_PARAMETER_CHANGED
        # row capturing the rejection. The success path's early-raise
        # stays in place (covered by the test above).
        async with operator_invocation(
            production_session_factory=production_session_factory,
            process_lifetime_id=PROCESS_LIFETIME_ID,
            operator_session_id_=operator_session_id("sess-1"),
            verb=ControlVerb.SWITCH_PROFILE,
        ) as handle:
            invocation_id = handle.invocation_id
            entry = write_operator_action_entry(
                handle=handle,
                verb=ControlVerb.SWITCH_PROFILE,
                parameters={"profile_name": "missing"},
                result=ControlResult.failure(
                    error_code=ControlErrorCode.NOT_FOUND,
                    error_detail="profile missing",
                ),
                now=_NOW,
            )
        assert entry is not None
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        assert rows[0].event_type == EventType.RISK_PARAMETER_CHANGED.value
        assert "missing" in rows[0].detail_json
        assert "not_found" in rows[0].detail_json

    async def test_run_universe_validation_writes_no_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with operator_invocation(
            production_session_factory=production_session_factory,
            process_lifetime_id=PROCESS_LIFETIME_ID,
            operator_session_id_=operator_session_id("sess-1"),
            verb=ControlVerb.RUN_UNIVERSE_VALIDATION,
        ) as handle:
            entry = write_operator_action_entry(
                handle=handle,
                verb=ControlVerb.RUN_UNIVERSE_VALIDATION,
                parameters={},
                result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
                now=_NOW,
            )
            invocation_id = handle.invocation_id
        assert entry is None
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert rows == []

    async def test_naive_now_rejected(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Deliberately naive — the test exercises the audit helper's
        # tzinfo-required guard. DTZ001 is suppressed for this single
        # construction so the rejection path is testable.
        naive = datetime(2026, 5, 26, 12, 0, 0)  # noqa: DTZ001
        async with operator_invocation(
            production_session_factory=production_session_factory,
            process_lifetime_id=PROCESS_LIFETIME_ID,
            operator_session_id_=operator_session_id("sess-1"),
            verb=ControlVerb.PAUSE,
        ) as handle:
            with pytest.raises(ValueError, match="tz-aware"):
                write_operator_action_entry(
                    handle=handle,
                    verb=ControlVerb.PAUSE,
                    parameters={"reason": "x"},
                    result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
                    now=naive,
                )


class TestAuditWriterFailurePath:
    async def test_failed_pause_still_writes_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        invocation_id = await _run_with_handle(
            production_session_factory,
            ControlVerb.PAUSE,
            parameters={"reason": "drawdown"},
            result=ControlResult.failure(
                error_code=ControlErrorCode.PRECONDITION_FAILED,
                error_detail="already paused",
            ),
        )
        rows = await _read_rows_for(production_session_factory, invocation_id)
        assert len(rows) == 1
        body = rows[0].detail_json
        assert "precondition_failed" in body
        assert "already paused" in body
