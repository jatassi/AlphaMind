"""Tests for ``command_center.control.proxy`` (story 04a / ALP-668)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.control import (
    ControlErrorCode,
    ControlResult,
)
from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.control.monitor_client import (
    FakeMonitorClient,
    MonitorForceClosePositionResult,
)
from alphamind.command_center.control.pipeline_client import (
    FakePipelineClient,
    PipelineSwitchProfileResult,
    PipelineTriggerEmergencyResult,
)
from alphamind.command_center.control.proxy import (
    ProxyContext,
    proxy_cancel_order,
    proxy_force_close_position,
    proxy_pause,
    proxy_resume,
    proxy_run_universe_validation,
    proxy_set_halt_mode,
    proxy_switch_profile,
    proxy_trigger_emergency_invocation,
)
from alphamind.portfolio_state.events.activity_log import EventType
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow

from .conftest import PROCESS_LIFETIME_ID

_FROZEN_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _ctx(factory: async_sessionmaker[AsyncSession]) -> ProxyContext:
    return ProxyContext(
        production_session_factory=factory,
        process_lifetime_id=PROCESS_LIFETIME_ID,
        operator_session_id=operator_session_id("sess-1"),
        now_factory=lambda: _FROZEN_NOW,
    )


async def _read_activity_rows(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> list[ActivityLogRow]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
            )
        ).scalars().all()
    return list(rows)


async def _read_invocation_row(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> InvocationRow | None:
    async with factory() as session:
        return await session.get(InvocationRow, invocation_id)


class TestProxyHappyPaths:
    async def test_pause_writes_row_and_returns_success(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        out = await proxy_pause(
            ctx=_ctx(production_session_factory), pipeline=pipeline, reason="drawdown"
        )
        assert out.result.ok is True
        assert pipeline.calls == [("pause", {"reason": "drawdown"})]
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        assert rows[0].event_type == EventType.RISK_PARAMETER_CHANGED.value

    async def test_resume_writes_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        out = await proxy_resume(
            ctx=_ctx(production_session_factory), pipeline=pipeline
        )
        assert out.result.ok is True
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1

    async def test_trigger_emergency_returns_pipeline_invocation_id(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        pipeline.set_trigger_emergency_response(
            PipelineTriggerEmergencyResult(
                result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
                invocation_id="inv-pipeline-7",
            )
        )
        out = await proxy_trigger_emergency_invocation(
            ctx=_ctx(production_session_factory),
            pipeline=pipeline,
            reason="ops triage",
        )
        assert out.result.ok is True
        assert out.pipeline_invocation_id == "inv-pipeline-7"
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        assert rows[0].event_type == EventType.EMERGENCY_INVOCATION_REQUESTED.value

    async def test_cancel_order_writes_row_with_order_id(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        monitor = FakeMonitorClient()
        out = await proxy_cancel_order(
            ctx=_ctx(production_session_factory), monitor=monitor, order_id="ord-1"
        )
        assert out.result.ok is True
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        assert rows[0].event_type == EventType.ORDER_CANCELLED.value
        assert rows[0].order_id == "ord-1"

    async def test_force_close_returns_envelope_id_and_writes_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        monitor = FakeMonitorClient()
        out = await proxy_force_close_position(
            ctx=_ctx(production_session_factory),
            monitor=monitor,
            position_id="pos-1",
            rationale="exit",
        )
        assert out.envelope_id == "MON.s1.42"
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        assert rows[0].position_id == "pos-1"
        assert "MON.s1.42" in rows[0].detail_json

    async def test_set_halt_mode_writes_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        monitor = FakeMonitorClient()
        out = await proxy_set_halt_mode(
            ctx=_ctx(production_session_factory),
            monitor=monitor,
            enabled=True,
            reason="vol spike",
        )
        assert out.result.ok is True
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        assert "halt_mode_enabled" in rows[0].detail_json

    async def test_run_universe_validation_returns_report_no_audit_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        out = await proxy_run_universe_validation(
            ctx=_ctx(production_session_factory), pipeline=pipeline
        )
        assert out.result.ok is True
        assert out.report is not None
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert rows == []


class TestProxySwitchProfile:
    async def test_switch_profile_writes_profile_switched_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        out = await proxy_switch_profile(
            ctx=_ctx(production_session_factory),
            pipeline=pipeline,
            profile_name="large",
        )
        assert out.result.ok is True
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        assert rows[0].event_type == EventType.PROFILE_SWITCHED.value

    async def test_switch_profile_no_op_writes_no_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        # FakePipelineClient default treats switch to ``medium`` as no-op.
        out = await proxy_switch_profile(
            ctx=_ctx(production_session_factory),
            pipeline=pipeline,
            profile_name="medium",
        )
        assert out.result.ok is True
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert rows == []

    async def test_switch_profile_failure_writes_no_row(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        pipeline.set_switch_profile_response(
            PipelineSwitchProfileResult(
                result=ControlResult.failure(
                    error_code=ControlErrorCode.NOT_FOUND,
                    error_detail="profile missing",
                )
            )
        )
        out = await proxy_switch_profile(
            ctx=_ctx(production_session_factory),
            pipeline=pipeline,
            profile_name="missing",
        )
        assert out.result.ok is False
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert rows == []
        # But the invocation row itself is still durable.
        invocation = await _read_invocation_row(
            production_session_factory, out.invocation_id
        )
        assert invocation is not None
        assert invocation.trigger_source == "operator_console"


class TestProxyFailurePaths:
    async def test_pause_failure_still_writes_row_with_error_state(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        pipeline.set_pause_response(
            ControlResult.failure(
                error_code=ControlErrorCode.PRECONDITION_FAILED,
                error_detail="already paused",
            )
        )
        out = await proxy_pause(
            ctx=_ctx(production_session_factory),
            pipeline=pipeline,
            reason="drawdown",
        )
        assert out.result.ok is False
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        assert "precondition_failed" in rows[0].detail_json
        assert "already paused" in rows[0].detail_json

    async def test_force_close_failure_writes_row_without_envelope_id(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        monitor = FakeMonitorClient()
        monitor.set_force_close_response(
            MonitorForceClosePositionResult(
                result=ControlResult.failure(
                    error_code=ControlErrorCode.NOT_FOUND,
                    error_detail="no such position",
                )
            )
        )
        out = await proxy_force_close_position(
            ctx=_ctx(production_session_factory),
            monitor=monitor,
            position_id="pos-1",
            rationale="exit",
        )
        assert out.envelope_id is None
        rows = await _read_activity_rows(production_session_factory, out.invocation_id)
        assert len(rows) == 1
        # The row's new_parameter_set_json captures the failure shape.
        assert "no such position" in rows[0].detail_json


class TestProxyInvocationRowMetadata:
    async def test_invocation_row_has_operator_console_trigger_source(
        self, production_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pipeline = FakePipelineClient()
        out = await proxy_pause(
            ctx=_ctx(production_session_factory), pipeline=pipeline, reason="x"
        )
        invocation = await _read_invocation_row(
            production_session_factory, out.invocation_id
        )
        assert invocation is not None
        assert invocation.trigger_source == "operator_console"
        assert "verb=pause" in invocation.trigger_reason
        assert "operator_session_id=sess-1" in invocation.trigger_reason


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
