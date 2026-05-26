"""Tests for ``command_center.alerts.rules`` (ALP-671 / story 05a).

Covers the rule + condition + outcome + snapshot value types: shape,
immutability, re-exported severity enum, condition Protocol fit-check.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from alphamind.command_center._kernel.ids import alert_rule_name
from alphamind.command_center.alerts.rules import (
    AlertCondition,
    AlertEvaluatorState,
    AlertOutcome,
    AlertRule,
    AlertSeverity,
)


class _StubCondition:
    """Minimal stand-in to verify the Protocol contract."""

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event, state
        return None


class TestAlertSeverity:
    def test_three_tier_vocabulary(self) -> None:
        assert AlertSeverity.CRITICAL.value == "critical"
        assert AlertSeverity.IMPORTANT.value == "important"
        assert AlertSeverity.OPERATIONAL.value == "operational"


class TestAlertRule:
    def test_constructs_with_all_fields(self) -> None:
        rule = AlertRule(
            name=alert_rule_name("pipeline_aborted"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=5),
            channels=("in_app", "discord"),
            condition=_StubCondition(),
        )
        assert rule.name == "pipeline_aborted"
        assert rule.severity is AlertSeverity.CRITICAL
        assert rule.debounce_window == timedelta(minutes=5)
        assert rule.channels == ("in_app", "discord")

    def test_is_frozen(self) -> None:
        rule = AlertRule(
            name=alert_rule_name("pipeline_aborted"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=5),
            channels=("in_app",),
            condition=_StubCondition(),
        )
        with pytest.raises((AttributeError, TypeError)):
            rule.severity = AlertSeverity.OPERATIONAL  # type: ignore[misc]


class TestAlertCondition:
    def test_stub_satisfies_protocol(self) -> None:
        condition: AlertCondition = _StubCondition()
        outcome = condition.evaluate(event=None, state=AlertEvaluatorState())
        assert outcome is None


class TestAlertEvaluatorState:
    def test_default_snapshot_has_empty_fields(self) -> None:
        state = AlertEvaluatorState()
        assert state.recent_activity_log_rows == ()
        assert state.portfolio_summary is None
        assert state.latest_invocation is None
        assert state.data_directory_size_bytes == 0
        assert state.extras == {}

    def test_snapshot_is_frozen(self) -> None:
        state = AlertEvaluatorState()
        with pytest.raises((AttributeError, TypeError)):
            state.data_directory_size_bytes = 42  # type: ignore[misc]


class TestAlertOutcome:
    def test_outcome_carries_primary_entity_and_context(self) -> None:
        outcome = AlertOutcome(
            primary_entity="inv-abc",
            context={"invocation_id": "inv-abc"},
        )
        assert outcome.primary_entity == "inv-abc"
        assert outcome.context == {"invocation_id": "inv-abc"}

    def test_outcome_is_frozen(self) -> None:
        outcome = AlertOutcome(primary_entity="x", context={"k": "v"})
        with pytest.raises((AttributeError, TypeError)):
            outcome.primary_entity = "y"  # type: ignore[misc]
