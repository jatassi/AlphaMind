"""Tests for the 17 default condition predicates (story 05a / ALP-671).

Each test covers one of the three predicate categories:

* Event-driven (PipelineAborted, MonitorWebsocketDisconnected,
  AgentMalformedOutput).
* State-polling (MarginCallDetected, HardBlockGuardrailRejection,
  HaltModeEntered, CommandAbandoned, ThesisResolved,
  DataDirectoryDiskPressure).
* Dormant (CriticalApiFailure, ImportantApiFailure,
  ScheduleMissCritical, ScheduleMiss, RegimeJump,
  OptionalDataCategorySkipped, DrawdownTierCrossed,
  ProfileBoundaryCrossed).

Plus the :func:`build_default_rules` factory shape assertions.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center._kernel.ids import alert_rule_name
from alphamind.command_center.alerts.conditions import (
    AgentMalformedOutputCondition,
    CommandAbandonedCondition,
    CriticalApiFailureCondition,
    DataDirectoryDiskPressureCondition,
    DormantCondition,
    DrawdownTierCrossedCondition,
    HaltModeEnteredCondition,
    HardBlockGuardrailRejectionCondition,
    ImportantApiFailureCondition,
    MarginCallDetectedCondition,
    MonitorWebsocketDisconnectedCondition,
    OptionalDataCategorySkippedCondition,
    PipelineAbortedCondition,
    ProfileBoundaryCrossedCondition,
    RegimeJumpCondition,
    ScheduleMissCondition,
    ScheduleMissCriticalCondition,
    ThesisResolvedCondition,
    build_default_rules,
)
from alphamind.command_center.alerts.rules import (
    AlertEvaluatorState,
    AlertSeverity,
)
from alphamind.persistence.models import Base
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory production-Base factory for state-polling predicates.
# ---------------------------------------------------------------------------


@pytest.fixture
async def state_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory production-Base factory seeded with a process_lifetime + invocation."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id="plt-test",
                process_role="monitor",
                process_start_at="2026-05-26T00:00:00Z",
                process_pid=42,
                hostname="t",
                git_sha="d",
                git_branch="t",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="x",
                claude_agent_sdk_version="x",
                os_release="t",
            )
        )
        session.add(
            InvocationRow(
                invocation_id="inv-test",
                process_lifetime_id="plt-test",
                start_at="2026-05-26T11:50:00Z",
                phase1_completed_at=None,
                phase2_completed_at=None,
                trigger_type="manual",
                trigger_source="operator_console",
                trigger_reason="test",
                active_mode="normal",
                git_sha_at_invocation="x",
                active_profile="x",
                active_regime="x",
                active_overlays_json="{}",
                resolved_config_hash="x",
                resolved_config_snapshot_path="x",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="x",
                data_source_freshness_json="{}",
                fill_collection_summary_json=None,
                command_execution_summary_json=None,
                staleness_flag=None,
                snapshot_metadata_json=None,
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_activity(
    factory: async_sessionmaker[AsyncSession],
    *,
    entry_id: str,
    event_type: str,
    entry_at: str,
    event_group: str,
    detail_json: str = "{}",
    position_id: str | None = None,
    thesis_id: str | None = None,
) -> None:
    async with factory() as session:
        session.add(
            ActivityLogRow(
                entry_id=entry_id,
                invocation_id="inv-test",
                entry_at=entry_at,
                event_type=event_type,
                event_group=event_group,
                source="OPERATOR_CONSOLE",
                detail_json=detail_json,
                position_id=position_id,
                order_id=None,
                thesis_id=thesis_id,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Event-driven predicates.
# ---------------------------------------------------------------------------


class TestPipelineAbortedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_failed_invocation_ended(self) -> None:
        cond = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_ENDED,
            payload={
                "invocation_id": "inv-bad",
                "status": "failed",
                "ended_at": "2026-05-26T12:00:00Z",
            },
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is True
        assert result.debounce_key == "inv-bad"
        assert result.context["invocation_id"] == "inv-bad"

    @pytest.mark.asyncio
    async def test_does_not_fire_on_succeeded_invocation_ended(self) -> None:
        cond = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_ENDED,
            payload={"invocation_id": "inv-ok", "status": "succeeded"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_on_unrelated_event(self) -> None:
        cond = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_STARTED,
            payload={"invocation_id": "inv-other"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_on_monitor_event(self) -> None:
        cond = PipelineAbortedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_CONNECTED,
            payload={},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_on_no_event(self) -> None:
        cond = PipelineAbortedCondition()
        result = await cond.evaluate(event=None, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False


class TestMonitorWebsocketDisconnectedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_websocket_disconnected(self) -> None:
        cond = MonitorWebsocketDisconnectedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_DISCONNECTED,
            payload={"session_id": "ws-1", "reason": "timeout"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is True
        assert result.debounce_key == "ws-1"

    @pytest.mark.asyncio
    async def test_does_not_fire_on_connected(self) -> None:
        cond = MonitorWebsocketDisconnectedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_CONNECTED,
            payload={"session_id": "ws-1"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False


class TestAgentMalformedOutputCondition:
    @pytest.mark.asyncio
    async def test_fires_on_malformed_output_retry(self) -> None:
        cond = AgentMalformedOutputCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.AGENT_RETRYING,
            payload={
                "agent_name": "synthesizer",
                "reason": "malformed_output: missing required field",
                "invocation_id": "inv-1",
            },
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is True
        assert result.debounce_key == "synthesizer"

    @pytest.mark.asyncio
    async def test_does_not_fire_on_non_malformed_retry(self) -> None:
        cond = AgentMalformedOutputCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.AGENT_RETRYING,
            payload={"agent_name": "synthesizer", "reason": "timeout"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False


# ---------------------------------------------------------------------------
# State-polling predicates.
# ---------------------------------------------------------------------------


class TestMarginCallDetectedCondition:
    @pytest.mark.asyncio
    async def test_fires_when_row_exists_inside_window(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="al-1",
            event_type="MARGIN_CALL",
            entry_at="2026-05-26T11:55:00Z",
            event_group="CASH_AND_MARGIN",
        )
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.debounce_key == "al-1"

    @pytest.mark.asyncio
    async def test_does_not_fire_when_no_rows(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_without_factory(self) -> None:
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_for_row_outside_window(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="al-old",
            event_type="MARGIN_CALL",
            entry_at="2026-05-26T08:00:00Z",  # 4h ago
            event_group="CASH_AND_MARGIN",
        )
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False


class TestHardBlockGuardrailRejectionCondition:
    @pytest.mark.asyncio
    async def test_fires_on_hard_block_row(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="gr-1",
            event_type="GUARDRAIL_REJECTION",
            entry_at="2026-05-26T11:55:00Z",
            event_group="RISK_AND_GUARDRAIL",
            detail_json='{"zone": "hard_block", "rule": "max_position_size"}',
            position_id=None,
        )
        cond = HardBlockGuardrailRejectionCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True

    @pytest.mark.asyncio
    async def test_does_not_fire_on_soft_block(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="gr-soft",
            event_type="GUARDRAIL_REJECTION",
            entry_at="2026-05-26T11:55:00Z",
            event_group="RISK_AND_GUARDRAIL",
            detail_json='{"zone": "soft_block"}',
        )
        cond = HardBlockGuardrailRejectionCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False


class TestHaltModeEnteredCondition:
    @pytest.mark.asyncio
    async def test_fires_on_halt_activated_row(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="halt-1",
            event_type="HALT_ACTIVATED",
            entry_at="2026-05-26T11:58:00Z",
            event_group="RISK_AND_GUARDRAIL",
        )
        cond = HaltModeEnteredCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.debounce_key == "halt-1"


class TestCommandAbandonedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_command_abandoned(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="cmd-1",
            event_type="COMMAND_ABANDONED",
            entry_at="2026-05-26T11:58:00Z",
            event_group="PM_DECISION",
        )
        cond = CommandAbandonedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True


class TestThesisResolvedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_thesis_resolved(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Need to seed theses row first for FK; alternatively use thesis_id=None.
        await _seed_activity(
            state_factory,
            entry_id="th-1",
            event_type="THESIS_RESOLVED",
            entry_at="2026-05-26T11:58:00Z",
            event_group="THESIS",
            thesis_id=None,
        )
        cond = ThesisResolvedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True


class TestDataDirectoryDiskPressureCondition:
    @pytest.mark.asyncio
    async def test_fires_when_size_exceeds_threshold(
        self,
        tmp_path: object,
    ) -> None:
        from pathlib import Path

        data_dir = Path(str(tmp_path)) / "data"
        data_dir.mkdir()
        # Create a file that exceeds a tiny threshold.
        big = data_dir / "big.bin"
        big.write_bytes(b"x" * 1024)
        cond = DataDirectoryDiskPressureCondition(
            data_dir=data_dir,
            threshold_bytes=100,
        )
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.context["size_bytes"] >= 1024

    @pytest.mark.asyncio
    async def test_does_not_fire_when_under_threshold(self, tmp_path: object) -> None:
        from pathlib import Path

        data_dir = Path(str(tmp_path)) / "data"
        data_dir.mkdir()
        (data_dir / "small.bin").write_bytes(b"x")
        cond = DataDirectoryDiskPressureCondition(
            data_dir=data_dir,
            threshold_bytes=1_000_000_000,
        )
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_when_directory_missing(self, tmp_path: object) -> None:
        from pathlib import Path

        missing = Path(str(tmp_path)) / "missing"
        cond = DataDirectoryDiskPressureCondition(
            data_dir=missing,
            threshold_bytes=0,
        )
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False


# ---------------------------------------------------------------------------
# Dormant predicates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cond_factory",
    [
        CriticalApiFailureCondition,
        ImportantApiFailureCondition,
        ScheduleMissCriticalCondition,
        ScheduleMissCondition,
        RegimeJumpCondition,
        OptionalDataCategorySkippedCondition,
        DrawdownTierCrossedCondition,
        ProfileBoundaryCrossedCondition,
    ],
)
@pytest.mark.asyncio
async def test_dormant_predicates_never_fire(cond_factory: type) -> None:
    cond = cond_factory()
    event = PipelineEvent(
        event_type=PipelineEventType.INVOCATION_ENDED,
        payload={"status": "failed"},
    )
    result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
    assert result.fired is False


def test_dormant_log_startup_warning_emits_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cond = DormantCondition(
        name=alert_rule_name("test_dormant"),
        missing_emit="test missing emit",
    )
    with caplog.at_level(logging.WARNING, logger="alphamind.command_center.alerts.conditions"):
        cond.log_startup_warning()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "test_dormant" in warnings[0].message
    assert "test missing emit" in warnings[0].message


# ---------------------------------------------------------------------------
# build_default_rules
# ---------------------------------------------------------------------------


class TestBuildDefaultRules:
    def test_returns_exactly_17_rules(self) -> None:
        rules = build_default_rules()
        assert len(rules) == 17

    def test_rule_names_are_unique(self) -> None:
        rules = build_default_rules()
        names = [str(r.name) for r in rules]
        assert len(set(names)) == 17

    def test_critical_rules_route_to_in_app_and_discord(self) -> None:
        rules = build_default_rules()
        for rule in rules:
            if rule.severity == AlertSeverity.CRITICAL:
                assert "in_app" in rule.channels
                assert "discord" in rule.channels

    def test_important_rules_route_to_in_app_and_discord(self) -> None:
        rules = build_default_rules()
        for rule in rules:
            if rule.severity == AlertSeverity.IMPORTANT:
                assert "in_app" in rule.channels
                assert "discord" in rule.channels

    def test_operational_rules_route_to_in_app_only(self) -> None:
        rules = build_default_rules()
        for rule in rules:
            if rule.severity == AlertSeverity.OPERATIONAL:
                assert rule.channels == ("in_app",)

    def test_debounce_overrides_apply(self) -> None:
        from alphamind.command_center.alerts.conditions import (
            DEFAULT_DEBOUNCE_WINDOWS,
        )

        override = timedelta(seconds=42)
        name = next(iter(DEFAULT_DEBOUNCE_WINDOWS))
        rules = build_default_rules(debounce_overrides={name: override})
        match = next(r for r in rules if r.name == name)
        assert match.debounce_window == override

    def test_channel_overrides_apply(self) -> None:
        from alphamind.command_center.alerts.conditions import (
            DEFAULT_DEBOUNCE_WINDOWS,
        )

        name = next(iter(DEFAULT_DEBOUNCE_WINDOWS))
        rules = build_default_rules(channel_overrides={name: ("in_app",)})
        match = next(r for r in rules if r.name == name)
        assert match.channels == ("in_app",)
