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
from alphamind.command_center.config import AlertsConfig
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

    @pytest.mark.asyncio
    async def test_debounce_table_prunes_expired_entries(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
    ) -> None:
        """Regression for finding #10 (Wave-5 review).

        Each distinct ``(rule_name, debounce_key)`` previously inserted
        a permanent entry into ``self._debounce``; with per-invocation
        debounce keys (e.g. PipelineAbortedCondition keyed by
        ``invocation_id``) the table grew monotonically. The engine now
        opportunistically evicts entries whose debounce window has
        elapsed; we drive ten distinct keys through one rule and assert
        the table contains exactly the most-recent live entry once the
        window passes.
        """
        cond = _AlwaysFiresCondition(debounce_key="initial")
        rule = AlertRule(
            name=alert_rule_name("pipeline_aborted"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=5),
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
        # Fire ten distinct keys.
        for i in range(10):
            cond.debounce_key = f"invocation-{i}"
            await engine.evaluate_once()
            fixed_clock.advance(timedelta(seconds=1))
        # All ten are still inside the 5-min debounce window.
        assert len(engine._debounce) == 10
        # Jump past the window and fire one more key — pruning evicts
        # the nine stale entries; the new fire writes one entry.
        fixed_clock.advance(timedelta(minutes=10))
        cond.debounce_key = "invocation-final"
        await engine.evaluate_once()
        keys_remaining = list(engine._debounce.keys())
        assert keys_remaining == [(rule.name, "invocation-final")]


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


# ---------------------------------------------------------------------------
# Hot-reload — story 06b / ALP-683.
# ---------------------------------------------------------------------------


def _alerts_yaml_with_rules(rules: list[dict[str, Any]]) -> str:
    """Render an alerts.yaml payload for the hot-reload tests.

    The channels block is held fixed; the rules list is the only knob
    the per-test fixtures vary. ``yaml.safe_dump`` would also work
    but a hand-rolled multi-line string keeps the field ordering
    obvious in the failure trace.
    """
    import yaml as _yaml

    payload = {
        "rules": rules,
        "channels": {"discord": {"webhook_url_env": "ALPHAMIND_DISCORD_WEBHOOK"}},
    }
    return _yaml.safe_dump(payload, sort_keys=False)


def _make_test_rule(name: str, debounce_minutes: int = 10) -> AlertRule:
    return AlertRule(
        name=alert_rule_name(name),
        severity=AlertSeverity.CRITICAL,
        debounce_window=timedelta(minutes=debounce_minutes),
        channels=("in_app",),
        condition=_NeverFiresCondition(),
    )


class TestHotReload:
    @pytest.mark.asyncio
    async def test_constructor_requires_both_path_and_builder(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        tmp_path: Any,
    ) -> None:
        # Wiring only the path without the builder is a configuration
        # mistake — fail loud at construction.
        rule = _make_test_rule("only_path")
        path = tmp_path / "alerts.yaml"
        path.write_text(_alerts_yaml_with_rules([]), encoding="utf-8")
        with pytest.raises(ValueError, match="alerts_config_path"):
            AlertEngine(
                rules=[rule],
                multiplexer=EventMultiplexer(),
                cc_writer_factory=cc_writer_factory,
                discord_channel=FakeDiscordChannel(),
                clock=fixed_clock,
                alerts_config_path=path,
            )

    @pytest.mark.asyncio
    async def test_unchanged_mtime_returns_none(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        tmp_path: Any,
    ) -> None:
        # First call records the mtime; second call (no edit) is a no-op.
        path = tmp_path / "alerts.yaml"
        path.write_text(_alerts_yaml_with_rules([]), encoding="utf-8")
        rule = _make_test_rule("baseline")

        def _builder(_config: AlertsConfig) -> tuple[AlertRule, ...]:
            return (rule,)

        engine = AlertEngine(
            rules=[rule],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
            alerts_config_path=path,
            rules_builder=_builder,
        )
        diff = await engine._reload_rules_from_file_if_changed()
        assert diff is None

    @pytest.mark.asyncio
    async def test_added_rule_appears_in_rule_set(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        tmp_path: Any,
    ) -> None:
        path = tmp_path / "alerts.yaml"
        path.write_text(_alerts_yaml_with_rules([]), encoding="utf-8")
        baseline = _make_test_rule("baseline")
        addition = _make_test_rule("addition")
        builder_state: dict[str, tuple[AlertRule, ...]] = {"return": (baseline,)}

        def _builder(_config: AlertsConfig) -> tuple[AlertRule, ...]:
            return builder_state["return"]

        engine = AlertEngine(
            rules=[baseline],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
            alerts_config_path=path,
            rules_builder=_builder,
        )
        # Mutate YAML mtime + arrange builder to surface the new rule.
        builder_state["return"] = (baseline, addition)
        # ``Path.touch`` advances mtime; sleep gives mtime_ns room to
        # tick past the constructor's stat() snapshot.
        await asyncio.sleep(0.01)
        path.touch()
        diff = await engine._reload_rules_from_file_if_changed()
        assert diff is not None
        assert diff.added == (alert_rule_name("addition"),)
        assert diff.removed == ()
        assert diff.updated == ()
        assert {str(r.name) for r in engine.rules} == {"baseline", "addition"}

    @pytest.mark.asyncio
    async def test_removed_rule_drops_debounce_entries(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        tmp_path: Any,
    ) -> None:
        path = tmp_path / "alerts.yaml"
        path.write_text(_alerts_yaml_with_rules([]), encoding="utf-8")
        keep = _make_test_rule("keep")
        drop = _make_test_rule("drop")
        builder_state: dict[str, tuple[AlertRule, ...]] = {"return": (keep, drop)}

        def _builder(_config: AlertsConfig) -> tuple[AlertRule, ...]:
            return builder_state["return"]

        engine = AlertEngine(
            rules=[keep, drop],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
            alerts_config_path=path,
            rules_builder=_builder,
        )
        # Seed a debounce entry on the rule we're about to remove.
        from alphamind.command_center.alerts.engine import _DebounceEntry

        engine._debounce[(drop.name, "key1")] = _DebounceEntry(last_fired_at=_NOW)
        # Edit YAML — builder now returns only ``keep``.
        builder_state["return"] = (keep,)
        await asyncio.sleep(0.01)
        path.touch()
        diff = await engine._reload_rules_from_file_if_changed()
        assert diff is not None
        assert diff.removed == (alert_rule_name("drop"),)
        # Debounce entry for the dropped rule is evicted.
        assert (drop.name, "key1") not in engine._debounce
        # Window registry no longer carries the dropped rule.
        assert drop.name not in engine._debounce_windows

    @pytest.mark.asyncio
    async def test_updated_rule_preserves_last_fired_at(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        tmp_path: Any,
    ) -> None:
        path = tmp_path / "alerts.yaml"
        path.write_text(_alerts_yaml_with_rules([]), encoding="utf-8")
        original = _make_test_rule("tunable", debounce_minutes=10)
        updated = _make_test_rule("tunable", debounce_minutes=99)
        builder_state: dict[str, tuple[AlertRule, ...]] = {"return": (original,)}

        def _builder(_config: AlertsConfig) -> tuple[AlertRule, ...]:
            return builder_state["return"]

        engine = AlertEngine(
            rules=[original],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
            alerts_config_path=path,
            rules_builder=_builder,
        )
        from alphamind.command_center.alerts.engine import _DebounceEntry

        engine._debounce[(original.name, "key1")] = _DebounceEntry(last_fired_at=_NOW)
        builder_state["return"] = (updated,)
        await asyncio.sleep(0.01)
        path.touch()
        diff = await engine._reload_rules_from_file_if_changed()
        assert diff is not None
        assert diff.updated == (alert_rule_name("tunable"),)
        # Last-fired-at is preserved — the debounce key still resolves
        # to the seeded entry.
        assert (original.name, "key1") in engine._debounce
        # Debounce window updated to the new value.
        assert engine._debounce_windows[updated.name] == timedelta(minutes=99)

    @pytest.mark.asyncio
    async def test_channels_section_change_publishes_restart_event(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        tmp_path: Any,
    ) -> None:
        from alphamind.command_center._kernel.events import ConfigReloadRequiresRestartEvent
        from alphamind.command_center.config import (
            AlertsChannels,
            AlertsConfig,
            DiscordChannelConfig,
        )

        path = tmp_path / "alerts.yaml"
        path.write_text(_alerts_yaml_with_rules([]), encoding="utf-8")
        rule = _make_test_rule("baseline")
        initial_config = AlertsConfig(
            rules=[],
            channels=AlertsChannels(discord=DiscordChannelConfig(webhook_url_env="ORIGINAL")),
        )

        def _builder(_config: AlertsConfig) -> tuple[AlertRule, ...]:
            return (rule,)

        multiplexer = EventMultiplexer()
        engine = AlertEngine(
            rules=[rule],
            multiplexer=multiplexer,
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
            alerts_config_path=path,
            rules_builder=_builder,
            initial_config=initial_config,
        )
        # Edit YAML to rename the env var — the rules list is identical
        # but the channels section changed.
        new_yaml = _alerts_yaml_with_rules([])
        new_yaml = new_yaml.replace("ALPHAMIND_DISCORD_WEBHOOK", "DIFFERENT_ENV")
        path.write_text(new_yaml, encoding="utf-8")
        await asyncio.sleep(0.01)
        path.touch()
        # Subscribe before triggering reload so the published event lands.
        async with multiplexer.subscribe() as queue:
            diff = await engine._reload_rules_from_file_if_changed()
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert diff is not None
        assert diff.non_rules_section_changed is True
        assert isinstance(event, ConfigReloadRequiresRestartEvent)
        assert event.filename == "alerts.yaml"

    @pytest.mark.asyncio
    async def test_concurrent_evaluate_sees_consistent_rule_set(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        fixed_clock: Any,
        tmp_path: Any,
    ) -> None:
        """Hot-reload must not race with evaluate-all rule iteration."""
        path = tmp_path / "alerts.yaml"
        path.write_text(_alerts_yaml_with_rules([]), encoding="utf-8")
        rule_a = _make_test_rule("a")
        rule_b = _make_test_rule("b")
        builder_state: dict[str, tuple[AlertRule, ...]] = {"return": (rule_a,)}

        def _builder(_config: AlertsConfig) -> tuple[AlertRule, ...]:
            return builder_state["return"]

        engine = AlertEngine(
            rules=[rule_a],
            multiplexer=EventMultiplexer(),
            cc_writer_factory=cc_writer_factory,
            discord_channel=FakeDiscordChannel(),
            clock=fixed_clock,
            alerts_config_path=path,
            rules_builder=_builder,
        )
        builder_state["return"] = (rule_a, rule_b)
        await asyncio.sleep(0.01)
        path.touch()

        # Reload + evaluate concurrently; the outcome list must reflect
        # one consistent snapshot (either one or two rules), never a
        # mid-mutation tear (a count that doesn't match either side).
        outcomes_task = asyncio.create_task(engine.evaluate_once())
        reload_task = asyncio.create_task(engine._reload_rules_from_file_if_changed())
        outcomes, diff = await asyncio.gather(outcomes_task, reload_task)
        assert len(outcomes) in {1, 2}
        assert diff is not None and diff.added == (alert_rule_name("b"),)


# Suppress unused-symbol warning.
_ = AlertRuleName
