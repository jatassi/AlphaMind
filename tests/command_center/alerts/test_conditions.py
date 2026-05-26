"""Tests for the 17 default alert condition predicates (ALP-671).

Each predicate fires under one specific upstream signal; the suite
covers the firing path + a representative non-firing path per
predicate. Dormant predicates (Q1/Q6/Q8 categorical, schedule
miss / important / optional, regime jump) emit one-time startup
WARNINGs naming the missing upstream emit point.
"""

from __future__ import annotations

import logging

import pytest

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center.alerts.conditions import (
    ALL_DEFAULT_CONDITIONS,
    AgentMalformedOutputCondition,
    CommandAbandonedCondition,
    CriticalApiFailureCondition,
    DataDirectoryDiskPressureCondition,
    DrawdownTierCrossedCondition,
    HaltModeEnteredCondition,
    HardBlockGuardrailRejectionCondition,
    ImportantApiFailureCondition,
    MarginCallCondition,
    MonitorWebsocketDisconnectedCondition,
    OptionalDataCategorySkippedCondition,
    PipelineAbortedCondition,
    ProfileBoundaryCrossedCondition,
    RegimeJumpCondition,
    ScheduleMissCondition,
    ScheduleMissCriticalCondition,
    ThesisResolvedCondition,
    log_dormant_predicates,
)
from alphamind.command_center.alerts.rules import AlertEvaluatorState

# ---------------------------------------------------------------------------
# Pipeline aborted (event-driven by invocation_ended status=failed)
# ---------------------------------------------------------------------------


class TestPipelineAbortedCondition:
    def test_fires_on_invocation_ended_failed(self) -> None:
        condition = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_ENDED,
            payload={"invocation_id": "inv-1", "status": "failed"},
        )
        outcome = condition.evaluate(event=event, state=AlertEvaluatorState())
        assert outcome is not None
        assert outcome.primary_entity == "inv-1"

    def test_skips_when_invocation_ended_succeeded(self) -> None:
        condition = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_ENDED,
            payload={"invocation_id": "inv-1", "status": "succeeded"},
        )
        assert condition.evaluate(event=event, state=AlertEvaluatorState()) is None

    def test_skips_when_event_is_none(self) -> None:
        condition = PipelineAbortedCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None


# ---------------------------------------------------------------------------
# Monitor websocket disconnected (event-driven by monitor SSE)
# ---------------------------------------------------------------------------


class TestMonitorWebsocketDisconnectedCondition:
    def test_fires_on_websocket_disconnected(self) -> None:
        condition = MonitorWebsocketDisconnectedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_DISCONNECTED,
            payload={"venue": "ibkr"},
        )
        outcome = condition.evaluate(event=event, state=AlertEvaluatorState())
        assert outcome is not None
        assert outcome.primary_entity == "ibkr"

    def test_skips_on_websocket_connected(self) -> None:
        condition = MonitorWebsocketDisconnectedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_CONNECTED,
            payload={"venue": "ibkr"},
        )
        assert condition.evaluate(event=event, state=AlertEvaluatorState()) is None


# ---------------------------------------------------------------------------
# Margin call (state-polling)
# ---------------------------------------------------------------------------


class TestMarginCallCondition:
    def test_fires_when_recent_margin_call_row_exists(self) -> None:
        state = AlertEvaluatorState(
            recent_activity_log_rows=(
                {
                    "event_type": "MARGIN_CALL",
                    "entry_id": "ent-1",
                    "account_id": "acct-1",
                },
            )
        )
        condition = MarginCallCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "acct-1"

    def test_skips_when_no_recent_rows(self) -> None:
        condition = MarginCallCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None


# ---------------------------------------------------------------------------
# Drawdown progressive tier crossed (state-polling)
# ---------------------------------------------------------------------------


class TestDrawdownTierCrossedCondition:
    def test_fires_when_drawdown_at_or_past_threshold(self) -> None:
        state = AlertEvaluatorState(
            portfolio_summary={
                "account_id": "acct-1",
                "cumulative_drawdown": -0.07,
            }
        )
        condition = DrawdownTierCrossedCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "acct-1"

    def test_skips_when_within_tier(self) -> None:
        state = AlertEvaluatorState(
            portfolio_summary={
                "account_id": "acct-1",
                "cumulative_drawdown": -0.02,
            }
        )
        condition = DrawdownTierCrossedCondition()
        assert condition.evaluate(event=None, state=state) is None


# ---------------------------------------------------------------------------
# Halt mode entered (state-polling)
# ---------------------------------------------------------------------------


class TestHaltModeEnteredCondition:
    def test_fires_on_recent_halt_activated_row(self) -> None:
        state = AlertEvaluatorState(
            recent_activity_log_rows=(
                {
                    "event_type": "HALT_ACTIVATED",
                    "entry_id": "ent-1",
                    "account_id": "acct-1",
                },
            )
        )
        condition = HaltModeEnteredCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "acct-1"

    def test_skips_when_no_recent_halt_row(self) -> None:
        condition = HaltModeEnteredCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None


# ---------------------------------------------------------------------------
# Hard-block guardrail rejection (state-polling)
# ---------------------------------------------------------------------------


class TestHardBlockGuardrailRejectionCondition:
    def test_fires_on_hard_block_zone_rejection(self) -> None:
        state = AlertEvaluatorState(
            recent_activity_log_rows=(
                {
                    "event_type": "GUARDRAIL_REJECTION",
                    "position_id": "pos-1",
                    "detail": {"zone": "hard_block"},
                },
            )
        )
        condition = HardBlockGuardrailRejectionCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "pos-1"

    def test_skips_when_zone_is_warning(self) -> None:
        state = AlertEvaluatorState(
            recent_activity_log_rows=(
                {
                    "event_type": "GUARDRAIL_REJECTION",
                    "position_id": "pos-1",
                    "detail": {"zone": "warning"},
                },
            )
        )
        condition = HardBlockGuardrailRejectionCondition()
        assert condition.evaluate(event=None, state=state) is None


# ---------------------------------------------------------------------------
# Agent malformed output (event-driven on agent_retrying)
# ---------------------------------------------------------------------------


class TestAgentMalformedOutputCondition:
    def test_fires_on_agent_retrying_with_malformed_reason(self) -> None:
        condition = AgentMalformedOutputCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.AGENT_RETRYING,
            payload={
                "agent_name": "analyst",
                "reason": "malformed_output",
                "invocation_id": "inv-1",
            },
        )
        outcome = condition.evaluate(event=event, state=AlertEvaluatorState())
        assert outcome is not None
        assert outcome.primary_entity == "analyst"

    def test_skips_on_other_retry_reason(self) -> None:
        condition = AgentMalformedOutputCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.AGENT_RETRYING,
            payload={
                "agent_name": "analyst",
                "reason": "timeout",
                "invocation_id": "inv-1",
            },
        )
        assert condition.evaluate(event=event, state=AlertEvaluatorState()) is None


# ---------------------------------------------------------------------------
# Data directory disk pressure (state-polling — filesystem size)
# ---------------------------------------------------------------------------


class TestDataDirectoryDiskPressureCondition:
    def test_fires_when_size_exceeds_threshold(self) -> None:
        state = AlertEvaluatorState(data_directory_size_bytes=2_000_000_000)
        condition = DataDirectoryDiskPressureCondition(threshold_bytes=1_000_000_000)
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "data_directory"

    def test_skips_when_under_threshold(self) -> None:
        state = AlertEvaluatorState(data_directory_size_bytes=500_000_000)
        condition = DataDirectoryDiskPressureCondition(threshold_bytes=1_000_000_000)
        assert condition.evaluate(event=None, state=state) is None


# ---------------------------------------------------------------------------
# Profile boundary crossed (state-polling)
# ---------------------------------------------------------------------------


class TestProfileBoundaryCrossedCondition:
    def test_fires_when_equity_falls_below_range(self) -> None:
        state = AlertEvaluatorState(
            portfolio_summary={
                "total_equity": 4_000,
                "active_profile_min_usd": 5_000,
                "active_profile_max_usd": 10_000,
                "active_profile": "starter",
            }
        )
        condition = ProfileBoundaryCrossedCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "starter"

    def test_fires_when_equity_above_range(self) -> None:
        state = AlertEvaluatorState(
            portfolio_summary={
                "total_equity": 15_000,
                "active_profile_min_usd": 5_000,
                "active_profile_max_usd": 10_000,
                "active_profile": "starter",
            }
        )
        condition = ProfileBoundaryCrossedCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None

    def test_skips_when_within_range(self) -> None:
        state = AlertEvaluatorState(
            portfolio_summary={
                "total_equity": 7_500,
                "active_profile_min_usd": 5_000,
                "active_profile_max_usd": 10_000,
                "active_profile": "starter",
            }
        )
        condition = ProfileBoundaryCrossedCondition()
        assert condition.evaluate(event=None, state=state) is None


# ---------------------------------------------------------------------------
# Command abandoned (state-polling)
# ---------------------------------------------------------------------------


class TestCommandAbandonedCondition:
    def test_fires_on_recent_command_abandoned_row(self) -> None:
        state = AlertEvaluatorState(
            recent_activity_log_rows=(
                {
                    "event_type": "COMMAND_ABANDONED",
                    "entry_id": "ent-1",
                },
            )
        )
        condition = CommandAbandonedCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "ent-1"


# ---------------------------------------------------------------------------
# Thesis resolved (state-polling)
# ---------------------------------------------------------------------------


class TestThesisResolvedCondition:
    def test_fires_on_recent_thesis_resolved_row(self) -> None:
        state = AlertEvaluatorState(
            recent_activity_log_rows=(
                {
                    "event_type": "THESIS_RESOLVED",
                    "thesis_id": "th-1",
                    "entry_id": "ent-1",
                },
            )
        )
        condition = ThesisResolvedCondition()
        outcome = condition.evaluate(event=None, state=state)
        assert outcome is not None
        assert outcome.primary_entity == "th-1"


# ---------------------------------------------------------------------------
# Dormant predicates (Q1/Q6/Q8, important API, schedule miss, regime jump,
# optional category) — always return None; engine logs a one-time WARNING
# naming the missing upstream emit point.
# ---------------------------------------------------------------------------


class TestDormantPredicates:
    def test_critical_api_failure_predicate_is_dormant(self) -> None:
        condition = CriticalApiFailureCondition()
        # Even with a recent invocation row present, the predicate stays
        # dormant — Q1/Q6/Q8 categorical-failure event does not yet emit.
        state = AlertEvaluatorState(
            latest_invocation={"invocation_id": "inv-1", "status": "succeeded"}
        )
        assert condition.evaluate(event=None, state=state) is None

    def test_important_api_failure_predicate_is_dormant(self) -> None:
        condition = ImportantApiFailureCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None

    def test_schedule_miss_critical_predicate_is_dormant(self) -> None:
        condition = ScheduleMissCriticalCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None

    def test_schedule_miss_predicate_is_dormant(self) -> None:
        condition = ScheduleMissCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None

    def test_regime_jump_predicate_is_dormant(self) -> None:
        condition = RegimeJumpCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None

    def test_optional_data_category_skipped_predicate_is_dormant(self) -> None:
        condition = OptionalDataCategorySkippedCondition()
        assert condition.evaluate(event=None, state=AlertEvaluatorState()) is None

    def test_dormant_startup_warning_names_each_predicate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="alphamind.command_center.alerts")
        log_dormant_predicates(
            (
                CriticalApiFailureCondition(),
                ImportantApiFailureCondition(),
                ScheduleMissCriticalCondition(),
                ScheduleMissCondition(),
                RegimeJumpCondition(),
                OptionalDataCategorySkippedCondition(),
            )
        )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 6
        joined = " ".join(r.getMessage() for r in warnings)
        assert "CriticalApiFailureCondition" in joined
        assert "ImportantApiFailureCondition" in joined
        assert "ScheduleMissCriticalCondition" in joined
        assert "RegimeJumpCondition" in joined

    def test_log_dormant_predicates_does_not_warn_for_live_predicates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="alphamind.command_center.alerts")
        log_dormant_predicates(
            (PipelineAbortedCondition(), MarginCallCondition(), HaltModeEnteredCondition())
        )
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


# ---------------------------------------------------------------------------
# ALL_DEFAULT_CONDITIONS — the 17-element tuple the engine wires by default.
# ---------------------------------------------------------------------------


class TestAllDefaultConditions:
    def test_seventeen_default_conditions_registered(self) -> None:
        assert len(ALL_DEFAULT_CONDITIONS) == 17

    def test_default_conditions_keyed_by_rule_name(self) -> None:
        for rule_name, condition in ALL_DEFAULT_CONDITIONS:
            assert isinstance(rule_name, str)
            assert hasattr(condition, "evaluate")

    def test_default_rule_names_are_unique(self) -> None:
        names = [rule_name for rule_name, _ in ALL_DEFAULT_CONDITIONS]
        assert len(set(names)) == len(names)
