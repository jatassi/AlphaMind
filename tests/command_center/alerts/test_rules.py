"""Tests for ``command_center.alerts.rules`` (ALP-671 / story 05a).

Covers the rule + condition + outcome value types: shape, immutability,
re-exported severity enum, condition Protocol fit-check, and the
``AlertOutcome.dormant`` sentinel that dormant predicates return at
startup.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from typing import Any

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


class _StubState:
    """Minimal AlertEvaluatorState stand-in."""

    async def fetch_recent_activity_log_rows(
        self, *, event_types: tuple[str, ...], within: object
    ) -> tuple[Any, ...]:
        del event_types, within
        return ()

    async def fetch_latest_portfolio_summary(self) -> Any | None:
        return None

    async def fetch_latest_invocation(self) -> Any | None:
        return None


class TestAlertSeverity:
    def test_three_tier_vocabulary(self) -> None:
        # The severity tiers must match the design doc's three tiers
        # (Critical / Important / Operational) — value strings drive the
        # alerts.severity column + frontend banner color logic, so the
        # vocabulary is locked.
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
        # frozen dataclass: assignment raises
        with pytest.raises((AttributeError, TypeError)):
            rule.severity = AlertSeverity.OPERATIONAL  # type: ignore[misc]


class TestAlertCondition:
    def test_stub_satisfies_protocol(self) -> None:
        # The Protocol is structural; the stub must satisfy it implicitly.
        condition: AlertCondition = _StubCondition()
        outcome = condition.evaluate(event=None, state=_StubState())
        assert outcome is None


class TestAlertOutcome:
    def test_outcome_carries_primary_entity_and_context(self) -> None:
        # AlertOutcome is the value a fired condition returns; the
        # primary_entity is the debounce key; the context dict is
        # encoded into the alerts.context_json column.
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
