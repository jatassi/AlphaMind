"""Tests for :mod:`alphamind.command_center.alerts.rules` (story 05a / ALP-671).

Cover the three primitives: :class:`AlertRule`, :class:`AlertCondition`
(Protocol shape), :class:`AlertConditionResult`, :class:`AlertEvaluatorState`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from alphamind.command_center._kernel.ids import alert_rule_name
from alphamind.command_center.alerts.rules import (
    AlertCondition,
    AlertConditionResult,
    AlertEvaluatorState,
    AlertRule,
    AlertSeverity,
)
from alphamind.command_center.events.multiplexer import CombinedEvent


class _StubCondition:
    """Duck-typed condition; verifies :class:`AlertCondition` is `runtime_checkable`."""

    name = alert_rule_name("stub_rule")

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event, state
        return AlertConditionResult(fired=False)


class TestAlertConditionResult:
    def test_default_debounce_key_is_empty(self) -> None:
        result = AlertConditionResult(fired=True)
        assert result.debounce_key == ""

    def test_default_context_is_empty_immutable_mapping(self) -> None:
        result = AlertConditionResult(fired=True)
        assert isinstance(result.context, MappingProxyType)
        assert dict(result.context) == {}

    def test_context_is_frozen(self) -> None:
        result = AlertConditionResult(fired=True, context={"foo": "bar"})
        with pytest.raises(TypeError):
            result.context["foo"] = "mutated"  # type: ignore[index]

    def test_context_does_not_reflect_external_mutations(self) -> None:
        """The proxy is constructed off a defensive copy."""
        source = {"foo": "bar"}
        result = AlertConditionResult(fired=True, context=source)
        source["foo"] = "mutated"
        assert result.context["foo"] == "bar"

    def test_existing_mapping_proxy_passes_through(self) -> None:
        proxy = MappingProxyType({"foo": "bar"})
        result = AlertConditionResult(fired=True, context=proxy)
        assert result.context is proxy


class TestAlertConditionProtocol:
    def test_stub_satisfies_protocol(self) -> None:
        assert isinstance(_StubCondition(), AlertCondition)


class TestAlertRule:
    def test_rule_is_frozen(self) -> None:
        rule = AlertRule(
            name=alert_rule_name("stub_rule"),
            severity=AlertSeverity.CRITICAL,
            debounce_window=timedelta(minutes=5),
            channels=("in_app", "discord"),
            condition=_StubCondition(),
        )
        with pytest.raises(AttributeError):
            rule.name = alert_rule_name("other")  # type: ignore[misc]


class TestAlertEvaluatorState:
    def test_holds_now_and_optional_factory(self) -> None:
        now = datetime(2026, 5, 26, tzinfo=UTC)
        state = AlertEvaluatorState(now=now)
        assert state.now == now
        assert state.foreign_reader_factory is None

    def test_threaded_factory_round_trips(self) -> None:
        sentinel = object()
        now = datetime(2026, 5, 26, tzinfo=UTC)
        state = AlertEvaluatorState(now=now, foreign_reader_factory=sentinel)
        assert state.foreign_reader_factory is sentinel
