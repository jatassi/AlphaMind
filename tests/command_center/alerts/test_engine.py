"""Tests for :mod:`alphamind.command_center.alerts.engine` (story 05a / ALP-671)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.events import (
    AlertFiredEvent,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center._kernel.ids import (
    AlertRuleName,
    alert_rule_name,
)
from alphamind.command_center.alerts.channels.discord import FakeDiscordChannel
from alphamind.command_center.alerts.conditions import (
    DormantCondition,
    PipelineAbortedCondition,
)
from alphamind.command_center.alerts.engine import (
    AlertEngine,
    EvaluationOutcome,
)
from alphamind.command_center.alerts.persistence import list_active
from alphamind.command_center.alerts.rules import (
    AlertConditionResult,
    AlertEvaluatorState,
    AlertRule,
    AlertSeverity,
)
from alphamind.command_center.events.multiplexer import (
    CombinedEvent,
    EventMultiplexer,
)

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test conditions.
# ---------------------------------------------------------------------------


@dataclass
class _AlwaysFiresCondition:
    """Condition that always returns ``fired=True``.

    Used to verify the engine's debounce + persistence + channel fan-out
    without depending on the production condition predicates.
    """

    debounce_key: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    call_count: int = 0

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event, state
        self.call_count += 1
        return AlertConditionResult(
            fired=True,
            debounce_key=self.debounce_key,
            context=dict(self.context),
        )


@dataclass
class _NeverFiresCondition:
    call_count: int = 0

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event, state
        self.call_count += 1
        return AlertConditionResult(fired=False)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_clock() -> Any:
    state: dict[str, datetime] = {"now": _NOW}

    def clock() -> datetime:
        return state["now"]

    def advance(delta: timedelta) -> None:
        state["now"] = state["now"] + delta

    clock.advance = advance  # type: ignore[attr-defined]
    return clock


# ---------------------------------------------------------------------------
# Engine basics.
# ---------------------------------------------------------------------------


class TestAlertEngineBasics:
    def test_requires_non_empty_rules(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        with pytest.raises(ValueError, match="rules"):
            AlertEngine(
                rules=[],
                multiplexer=EventMultiplexer(),
                cc_writer_factory=cc_writer_factory,
                discord_channel=FakeDiscordChannel(),
            )

    def test_requires_positive_poll_interval(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rule = AlertRule(
            name=alert_rule_name("test"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(seconds=1),
            channels=("in_app",),
            condition=_NeverFiresCondition(),
        )
        with pytest.raises(ValueError, match="poll_interval"):
            AlertEngine(
                rules=[rule],
                multiplexer=EventMultiplexer(),
                cc_writer_factory=cc_writer_factory,
                discord_channel=FakeDiscordChannel(),
                poll_interval_seconds=0,
            )


# ---------------------------------------------------------------------------
# Evaluate-once: fire / persistence / fanout.
# ---------------------------------------------------------------------------


class TestEvaluateOnceFire:
    @pytest.mark.asyncio
    async def test_fire_persists_row_and_dispatches_channels(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        cond = _AlwaysFiresCondition(context={"foo": "bar"})
        rule = AlertRule(
            name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app", "discord"),
            condition=cond,
        )
        discord = FakeDiscordChannel()
        multiplexer = EventMultiplexer()
        engine = AlertEngine(
            rules=[rule],
            multiplexer=multiplexer,
            cc_writer_factory=cc_writer_factory,
            discord_channel=discord,
            clock=fixed_clock,
        )
        async with multiplexer.subscribe() as queue:
            outcomes = await engine.evaluate_once()
            in_app_event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.fired is True
        assert outcome.suppressed_by_debounce is False
        assert outcome.alert_id_ is not None
        assert len(discord.calls) == 1
        assert discord.calls[0]["rule_name"] == alert_rule_name("test_rule")
        assert isinstance(in_app_event, AlertFiredEvent)
        assert in_app_event.rule_name == "test_rule"
        # Row persisted with correct context.
        active = await list_active(cc_writer_factory, now=_NOW)
        assert len(active) == 1
        persisted_context = json.loads(active[0].context_json)
        assert persisted_context == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_operational_severity_skips_discord(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        rule = AlertRule(
            name=alert_rule_name("operational_rule"),
            severity=AlertSeverity.OPERATIONAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app",),
            condition=_AlwaysFiresCondition(),
        )
        discord = FakeDiscordChannel()
        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=discord,
            clock=fixed_clock,
        )
        await engine.evaluate_once()
        assert discord.calls == []

    @pytest.mark.asyncio
    async def test_no_fire_persists_no_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        rule = AlertRule(
            name=alert_rule_name("quiet_rule"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app", "discord"),
            condition=_NeverFiresCondition(),
        )
        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
        )
        outcomes = await engine.evaluate_once()
        assert outcomes[0].fired is False
        active = await list_active(cc_writer_factory, now=_NOW)
        assert active == []


# ---------------------------------------------------------------------------
# Debounce semantics.
# ---------------------------------------------------------------------------


class TestDebounce:
    @pytest.mark.asyncio
    async def test_two_fires_in_window_collapse_to_one_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        rule = AlertRule(
            name=alert_rule_name("debounce_rule"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app",),
            condition=_AlwaysFiresCondition(),
        )
        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
        )
        await engine.evaluate_once()
        # Advance only 1 second; well inside the 10-min window.
        fixed_clock.advance(timedelta(seconds=1))
        outcomes = await engine.evaluate_once()
        assert outcomes[0].fired is True
        assert outcomes[0].suppressed_by_debounce is True
        active = await list_active(cc_writer_factory, now=fixed_clock())
        assert len(active) == 1

    @pytest.mark.asyncio
    async def test_fire_outside_window_produces_second_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        rule = AlertRule(
            name=alert_rule_name("debounce_rule"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app",),
            condition=_AlwaysFiresCondition(),
        )
        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
        )
        await engine.evaluate_once()
        fixed_clock.advance(timedelta(minutes=11))
        outcomes = await engine.evaluate_once()
        assert outcomes[0].suppressed_by_debounce is False
        active = await list_active(cc_writer_factory, now=fixed_clock())
        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_distinct_debounce_keys_do_not_share_window(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        cond = _AlwaysFiresCondition(debounce_key="key-a")
        rule = AlertRule(
            name=alert_rule_name("debounce_rule"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app",),
            condition=cond,
        )
        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
        )
        await engine.evaluate_once()
        # Same window — but flip the condition's debounce key.
        cond.debounce_key = "key-b"
        fixed_clock.advance(timedelta(seconds=1))
        outcomes = await engine.evaluate_once()
        assert outcomes[0].suppressed_by_debounce is False
        active = await list_active(cc_writer_factory, now=fixed_clock())
        assert len(active) == 2


# ---------------------------------------------------------------------------
# Per-rule isolation.
# ---------------------------------------------------------------------------


class TestPerRuleIsolation:
    @pytest.mark.asyncio
    async def test_condition_raise_does_not_crash_engine(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        @dataclass
        class _Boom:
            async def evaluate(
                self,
                *,
                event: CombinedEvent | None,
                state: AlertEvaluatorState,
            ) -> AlertConditionResult:
                del event, state
                msg = "exploded"
                raise RuntimeError(msg)

        cond_ok = _AlwaysFiresCondition()
        rules = [
            AlertRule(
                name=alert_rule_name("boom"),
                severity=AlertSeverity.CRITICAL,
                debounce_window=timedelta(minutes=10),
                channels=("in_app",),
                condition=_Boom(),
            ),
            AlertRule(
                name=alert_rule_name("ok"),
                severity=AlertSeverity.CRITICAL,
                debounce_window=timedelta(minutes=10),
                channels=("in_app",),
                condition=cond_ok,
            ),
        ]
        engine = AlertEngine(
            rules=rules,
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
        )
        outcomes = await engine.evaluate_once()
        boom_out = next(o for o in outcomes if str(o.rule_name) == "boom")
        ok_out = next(o for o in outcomes if str(o.rule_name) == "ok")
        assert boom_out.fired is False
        assert ok_out.fired is True


# ---------------------------------------------------------------------------
# Run / stop.
# ---------------------------------------------------------------------------


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_run_processes_published_event_then_stops(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        rule = AlertRule(
            name=alert_rule_name("pipeline_aborted"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app",),
            condition=PipelineAbortedCondition(),
        )
        multiplexer = EventMultiplexer()
        discord = FakeDiscordChannel()
        engine = AlertEngine(
            rules=[rule],
            multiplexer=multiplexer,
            cc_writer_factory=cc_writer_factory,
            discord_channel=discord,
            poll_interval_seconds=60.0,
            clock=fixed_clock,
        )

        async def _drive() -> None:
            # Give the engine a tick to subscribe before publishing.
            await asyncio.sleep(0.05)
            await multiplexer.publish(
                PipelineEvent(
                    event_type=PipelineEventType.INVOCATION_ENDED,
                    payload={"invocation_id": "inv-1", "status": "failed"},
                )
            )
            # Wait for the engine's consumer to drain.
            await asyncio.sleep(0.2)
            engine.request_stop()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(engine.run())
            tg.create_task(_drive())
        active = await list_active(cc_writer_factory, now=fixed_clock())
        assert len(active) == 1

    @pytest.mark.asyncio
    async def test_run_stops_cleanly_on_cancellation(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        rule = AlertRule(
            name=alert_rule_name("quiet"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=10),
            channels=("in_app",),
            condition=_NeverFiresCondition(),
        )
        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            poll_interval_seconds=60.0,
            clock=fixed_clock,
        )
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, BaseExceptionGroup)):
            await task


# ---------------------------------------------------------------------------
# Dormant warnings.
# ---------------------------------------------------------------------------


class TestDormantStartupWarnings:
    @pytest.mark.asyncio
    async def test_dormant_rules_log_warning_at_startup(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dormant = DormantCondition(
            name=alert_rule_name("dormant_rule"),
            missing_emit="missing emit",
        )
        rule = AlertRule(
            name=alert_rule_name("dormant_rule"),
            severity=AlertSeverity.IMPORTANT,
            debounce_window=timedelta(minutes=10),
            channels=("in_app",),
            condition=dormant,
        )
        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
        )
        with caplog.at_level(logging.WARNING, logger="alphamind.command_center.alerts.conditions"):
            engine.log_dormant_startup_warnings()
        assert any("dormant_rule" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Outcome assertions.
# ---------------------------------------------------------------------------


def test_evaluation_outcome_default_fields() -> None:
    outcome = EvaluationOutcome(rule_name=alert_rule_name("x"), fired=False)
    assert outcome.alert_id_ is None
    assert outcome.suppressed_by_debounce is False
    assert outcome.context == {}


# Suppress unused-symbol warning.
_ = AlertRuleName
