"""Tests for the ``EmergencyTriggerEvaluator`` callback (story 04b / ALP-439).

The evaluator implements the ``on_emergency_input: Callable[[BreachLoopResult],
Awaitable[None]]`` callback the breach loop (story 03b) awaits exactly once
per tick. It maintains per-session rolling windows of regime classifications
and ``DrawdownSample`` observations, composes the four trigger evaluators
via ``evaluate_emergency_invocation``, enforces the cooldown via
``CooldownTracker``, and writes an ``EMERGENCY_INVOCATION_REQUESTED``
activity-log entry on fire.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.trigger_ids import (
    TriggerIdGenerator,
)
from alphamind.execution.continuous_monitor.emergency_trigger.cooldown import (
    CooldownTracker,
)
from alphamind.execution.continuous_monitor.emergency_trigger.evaluator import (
    EmergencyTriggerEvaluator,
    MarginCallObserver,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.guardrail_enforcement import Phase1EnforcementResult
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EmergencyInvocationRequestedDetail,
    EventGroup,
    EventSource,
    EventType,
)
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    DrawdownSample,
    MarginCallEvent,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T143000Z-abcdef01",
        started_at=_now(),
        mode="paper",
    )


def _breach_behavior_config(*, cooldown_minutes: int = 30) -> BreachBehaviorConfig:
    return BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=cooldown_minutes,
    )


def _active_risk_parameters(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    daily_limit_pct: float = 5.0,
) -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="daily_drawdown_pct",
                value=daily_limit_pct,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=daily_limit_pct,
            ),
        ),
        active_overlays=(),
    )


def _phase1_result(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    daily_limit_pct: float = 5.0,
) -> Phase1EnforcementResult:
    return Phase1EnforcementResult(
        active_risk_parameters=_active_risk_parameters(
            regime_label=regime_label,
            daily_limit_pct=daily_limit_pct,
        ),
        drawdown_tier=None,
    )


def _rule_evaluation(
    rule_id: str,
    *,
    zone: RiskZone,
    classification: BreachResponse | None,
) -> RuleEvaluation:
    return RuleEvaluation(
        rule_id=rule_id,
        current_value=100.0,
        limit_value=100.0,
        overage=0.0,
        zone=zone,
        classification=classification,
    )


def _result(
    *,
    as_of: datetime | None = None,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    rule_evaluations: tuple[RuleEvaluation, ...] = (),
    intraday_drawdown_pct: float = 0.0,
    daily_limit_pct: float = 5.0,
) -> BreachLoopResult:
    when = as_of if as_of is not None else _now()
    return BreachLoopResult(
        as_of=when,
        phase1_result=_phase1_result(
            regime_label=regime_label,
            daily_limit_pct=daily_limit_pct,
        ),
        rule_evaluations=rule_evaluations,
        halt_state=None,
        immediate_action_breaches=tuple(
            e for e in rule_evaluations if e.classification is BreachResponse.immediate_engine
        ),
        drawdown_velocity_sample=DrawdownSample(
            sampled_at=when,
            intraday_drawdown_pct=intraday_drawdown_pct,
        ),
    )


@dataclass
class _FakeMarginCallObserver:
    """Returns the configured event sequence one per call.

    Returns ``None`` once the explicit sequence is exhausted — never
    repeats the last event. This lets a test pin "first 2 ticks: no
    margin; 3rd tick: margin" by passing ``[None, None, event]``.
    """

    events: list[MarginCallEvent | None] = field(default_factory=list)
    calls: int = 0

    async def __call__(self) -> MarginCallEvent | None:
        idx = self.calls
        self.calls += 1
        if idx >= len(self.events):
            return None
        return self.events[idx]


@dataclass
class _RecordingWriter:
    """Captures every entry the evaluator emits without touching SQL."""

    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def __call__(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


async def _default_invocation_id_provider() -> str:
    return "inv-test-001"


def _make_evaluator(
    *,
    breach_behavior_config: BreachBehaviorConfig | None = None,
    margin_observer: MarginCallObserver | None = None,
    writer: _RecordingWriter | None = None,
    trigger_ids: TriggerIdGenerator | None = None,
    invocation_id_provider: Callable[[], Awaitable[str]] | None = None,
) -> tuple[EmergencyTriggerEvaluator, _RecordingWriter]:
    cfg = breach_behavior_config or _breach_behavior_config()
    cooldown = CooldownTracker(cooldown_minutes=cfg.emergency_invocation_cooldown_minutes)
    log = writer or _RecordingWriter()
    observer = margin_observer or _FakeMarginCallObserver()
    rule_lookup = MappingProxyType(
        {
            "net_long_pct": BreachResponse.deferred_to_pm,
            "sector_concentration_pct": BreachResponse.deferred_to_pm,
            "options_delta_pct": BreachResponse.deferred_to_pm,
            "total_short_pct": BreachResponse.deferred_to_pm,
        }
    )
    evaluator = EmergencyTriggerEvaluator(
        session=_session(),
        breach_behavior_config=cfg,
        cooldown=cooldown,
        trigger_ids=trigger_ids or TriggerIdGenerator(session_id=_session().session_id),
        margin_call_observer=observer,
        activity_log_writer=log,
        breach_response_lookup=rule_lookup,
        invocation_id_provider=invocation_id_provider or _default_invocation_id_provider,
    )
    return evaluator, log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_fire_when_inputs_are_clean() -> None:
    """No regime jump, no breaches, no margin call → no entry written."""
    evaluator, log = _make_evaluator()
    await evaluator.handle_emergency_input(_result())
    assert log.entries == []


@pytest.mark.asyncio
async def test_regime_jump_emits_one_entry() -> None:
    """Two ticks: NORMAL then CRISIS → regime_jump entry on the second tick."""
    evaluator, log = _make_evaluator()
    t0 = _now()
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.CRISIS)
    )

    assert len(log.entries) == 1
    entry = log.entries[0]
    assert isinstance(entry.detail, EmergencyInvocationRequestedDetail)
    assert entry.detail.trigger_type == "regime_jump"
    assert "regime jump" in entry.detail.trigger_reason.lower()
    assert entry.event_type is EventType.EMERGENCY_INVOCATION_REQUESTED
    assert entry.event_group is EventGroup.RISK_AND_GUARDRAIL
    assert entry.source is EventSource.GUARDRAIL_LAYER


@pytest.mark.asyncio
async def test_adjacent_regime_change_does_not_fire() -> None:
    """A one-step regime change (NORMAL → ELEVATED) does not fire."""
    evaluator, log = _make_evaluator()
    t0 = _now()
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.ELEVATED)
    )
    assert log.entries == []


@pytest.mark.asyncio
async def test_multi_rule_breach_emits_one_entry() -> None:
    """Three+ deferred-rule HARD_BLOCKs → multi_rule_breach entry."""
    evaluator, log = _make_evaluator()
    rules = (
        _rule_evaluation(
            "net_long_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.deferred_to_pm,
        ),
        _rule_evaluation(
            "sector_concentration_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.deferred_to_pm,
        ),
        _rule_evaluation(
            "options_delta_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.deferred_to_pm,
        ),
    )
    await evaluator.handle_emergency_input(_result(rule_evaluations=rules))

    assert len(log.entries) == 1
    assert log.entries[0].detail.trigger_type == "multi_rule_breach"


@pytest.mark.asyncio
async def test_two_deferred_blocks_does_not_fire_multi_rule() -> None:
    """Fewer than the configured count → no fire."""
    evaluator, log = _make_evaluator()
    rules = (
        _rule_evaluation(
            "net_long_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.deferred_to_pm,
        ),
        _rule_evaluation(
            "sector_concentration_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.deferred_to_pm,
        ),
    )
    await evaluator.handle_emergency_input(_result(rule_evaluations=rules))
    assert log.entries == []


@pytest.mark.asyncio
async def test_immediate_engine_blocks_do_not_count_toward_multi_rule() -> None:
    """``immediate_engine`` rules are dispatched via the cascade; not counted here."""
    evaluator, log = _make_evaluator()
    rules = (
        _rule_evaluation(
            "position_max_loss_equity_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.immediate_engine,
        ),
        _rule_evaluation(
            "daily_drawdown_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.immediate_engine,
        ),
        _rule_evaluation(
            "cumulative_drawdown_pct",
            zone=RiskZone.BLOCKED,
            classification=BreachResponse.immediate_engine,
        ),
    )
    await evaluator.handle_emergency_input(_result(rule_evaluations=rules))
    assert log.entries == []


@pytest.mark.asyncio
async def test_drawdown_velocity_fires_when_threshold_crossed_in_window() -> None:
    """A sample below threshold followed by one above within the window fires."""
    evaluator, log = _make_evaluator()
    t0 = _now()
    # Below threshold: 60% of 5.0 = 3.0; first sample at 1.0%.
    await evaluator.handle_emergency_input(
        _result(as_of=t0, intraday_drawdown_pct=1.0, daily_limit_pct=5.0)
    )
    # Above threshold 10 minutes later: 4.0%.
    await evaluator.handle_emergency_input(
        _result(
            as_of=t0 + timedelta(minutes=10),
            intraday_drawdown_pct=4.0,
            daily_limit_pct=5.0,
        )
    )

    assert len(log.entries) == 1
    assert log.entries[0].detail.trigger_type == "drawdown_velocity"


@pytest.mark.asyncio
async def test_drawdown_velocity_does_not_fire_outside_window() -> None:
    """Crossing the threshold more than ``window_minutes`` later does not fire."""
    evaluator, log = _make_evaluator()
    t0 = _now()
    await evaluator.handle_emergency_input(
        _result(as_of=t0, intraday_drawdown_pct=1.0, daily_limit_pct=5.0)
    )
    await evaluator.handle_emergency_input(
        _result(
            as_of=t0 + timedelta(minutes=45),
            intraday_drawdown_pct=4.0,
            daily_limit_pct=5.0,
        )
    )
    assert log.entries == []


@pytest.mark.asyncio
async def test_margin_call_fires_with_cooldown_remaining_zero() -> None:
    """An active margin call fires and bypasses the cooldown."""
    observer = _FakeMarginCallObserver(
        events=[
            MarginCallEvent(
                issued_at=_now(),
                additional_margin_required_usd=12_500.0,
            ),
        ]
    )
    evaluator, log = _make_evaluator(margin_observer=observer)
    await evaluator.handle_emergency_input(_result())

    assert len(log.entries) == 1
    entry = log.entries[0]
    assert entry.detail.trigger_type == "margin_call"
    assert entry.detail.cooldown_remaining_seconds == 0


@pytest.mark.asyncio
async def test_margin_call_bypasses_active_cooldown() -> None:
    """A margin call fires even when a prior non-margin emit is within the window."""
    observer = _FakeMarginCallObserver(
        events=[
            None,  # tick 1: seed regime, no margin
            None,  # tick 2: regime jump fires, no margin
            MarginCallEvent(  # tick 3: margin call bypasses cooldown
                issued_at=_now() + timedelta(minutes=2),
                additional_margin_required_usd=12_500.0,
            ),
        ]
    )
    evaluator, log = _make_evaluator(margin_observer=observer)
    t0 = _now()
    # First: regime jump from NORMAL → CRISIS to seed the cooldown.
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.CRISIS)
    )
    # Now a margin call fires inside the 30-min window.
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=2), regime_label=RegimeLabel.CRISIS)
    )

    assert len(log.entries) == 2
    assert log.entries[0].detail.trigger_type == "regime_jump"
    assert log.entries[1].detail.trigger_type == "margin_call"
    assert log.entries[1].detail.cooldown_remaining_seconds == 0


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_non_margin_trigger() -> None:
    """A second non-margin trigger inside the window emits nothing."""
    evaluator, log = _make_evaluator()
    t0 = _now()
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.CRISIS)
    )
    # Second crisis jump 5 minutes later (still inside the 30-min window).
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=6), regime_label=RegimeLabel.NORMAL)
    )
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=7), regime_label=RegimeLabel.CRISIS)
    )

    assert len(log.entries) == 1


@pytest.mark.asyncio
async def test_cooldown_source_is_breach_behavior_config_45_minutes() -> None:
    """With ``cooldown_minutes=45``, suppression spans 45 minutes (not the default 30)."""
    cfg = _breach_behavior_config(cooldown_minutes=45)
    evaluator, log = _make_evaluator(breach_behavior_config=cfg)
    t0 = _now()
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.CRISIS)
    )
    # 35 minutes after first fire — would lift under 30-min cooldown, suppressed under 45.
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=35), regime_label=RegimeLabel.NORMAL)
    )
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=36), regime_label=RegimeLabel.CRISIS)
    )

    assert len(log.entries) == 1


@pytest.mark.asyncio
async def test_determinism_two_identical_runs_produce_identical_decisions() -> None:
    """Identical input sequences produce identical fire decisions."""

    async def _run() -> list[str]:
        evaluator, log = _make_evaluator()
        t0 = _now()
        await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
        await evaluator.handle_emergency_input(
            _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.CRISIS)
        )
        return [e.detail.trigger_type for e in log.entries]

    first = await _run()
    second = await _run()
    assert first == second == ["regime_jump"]


@pytest.mark.asyncio
async def test_trigger_id_appears_in_entry_id() -> None:
    """Entries carry a session-scoped trigger ID via ``TriggerIdGenerator``."""
    session = _session()
    gen = TriggerIdGenerator(session_id=session.session_id)
    evaluator, log = _make_evaluator(trigger_ids=gen)
    t0 = _now()
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.CRISIS)
    )

    assert len(log.entries) == 1
    # Generator hands out monotonic integers — entry_id contains it.
    assert session.session_id in log.entries[0].entry_id
    # ``mon-emt-`` prefix groups emergency-trigger entries within the
    # continuous-monitor ``mon-`` vocabulary.
    assert log.entries[0].entry_id.startswith("mon-emt-")


@pytest.mark.asyncio
async def test_trigger_id_generator_monotonic_across_two_emits() -> None:
    """Two emits in one session pull two distinct monotonic IDs."""
    cfg = _breach_behavior_config(cooldown_minutes=1)
    evaluator, log = _make_evaluator(breach_behavior_config=cfg)
    t0 = _now()
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(seconds=1), regime_label=RegimeLabel.CRISIS)
    )
    # Cool down lifts after 1 minute; emit a second.
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=2), regime_label=RegimeLabel.NORMAL)
    )
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=2, seconds=1), regime_label=RegimeLabel.CRISIS)
    )

    assert len(log.entries) == 2
    assert log.entries[0].entry_id != log.entries[1].entry_id


def test_trigger_id_generator_shape() -> None:
    """The generator returns monotonic integer-suffixed IDs scoped by session."""
    gen = TriggerIdGenerator(session_id="mon-20260511T143000Z-abcdef01")
    first = gen.next()
    second = gen.next()
    assert second > first
    assert isinstance(first, int)
    assert isinstance(second, int)


@pytest.mark.asyncio
async def test_suppressed_emit_does_not_advance_window() -> None:
    """A suppressed (would-fire) non-margin trigger does NOT record a fire.

    Otherwise an in-window dropped jump would extend the cooldown silently,
    making the next legitimate fire even later. Suppression is a no-op on
    the tracker.
    """
    evaluator, log = _make_evaluator()
    t0 = _now()
    # First fire at t0+1m.
    await evaluator.handle_emergency_input(_result(as_of=t0, regime_label=RegimeLabel.NORMAL))
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=1), regime_label=RegimeLabel.CRISIS)
    )
    # Suppressed in-window jump at t0+10m.
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=9), regime_label=RegimeLabel.NORMAL)
    )
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=10), regime_label=RegimeLabel.CRISIS)
    )
    # 31 minutes after the first fire — window from first fire has elapsed.
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=31), regime_label=RegimeLabel.NORMAL)
    )
    await evaluator.handle_emergency_input(
        _result(as_of=t0 + timedelta(minutes=32), regime_label=RegimeLabel.CRISIS)
    )

    # Two genuine fires, one suppressed.
    assert len(log.entries) == 2
