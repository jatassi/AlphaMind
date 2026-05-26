"""Tests for :mod:`alphamind.command_center.alerts.channels.in_app` (story 05a / ALP-671)."""

from __future__ import annotations

import asyncio

import pytest

from alphamind.command_center._kernel.events import AlertFiredEvent
from alphamind.command_center._kernel.ids import alert_id, alert_rule_name
from alphamind.command_center.alerts.channels.in_app import InAppChannel
from alphamind.command_center.events.multiplexer import EventMultiplexer
from alphamind.command_center.persistence.codecs import AlertSeverity


@pytest.mark.asyncio
async def test_in_app_publishes_alert_fired_event() -> None:
    multiplexer = EventMultiplexer()
    channel = InAppChannel(multiplexer=multiplexer)
    async with multiplexer.subscribe() as queue:
        await channel.send(
            alert_id_=alert_id("alert-1"),
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            context={"foo": "bar"},
        )
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert isinstance(event, AlertFiredEvent)
    assert event.alert_id == "alert-1"
    assert event.rule_name == "test_rule"
    assert event.severity == "critical"
    assert event.payload["foo"] == "bar"


@pytest.mark.asyncio
async def test_in_app_event_fanout_to_multiple_subscribers() -> None:
    multiplexer = EventMultiplexer()
    channel = InAppChannel(multiplexer=multiplexer)
    async with multiplexer.subscribe() as queue_a, multiplexer.subscribe() as queue_b:
        await channel.send(
            alert_id_=alert_id("alert-2"),
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context={},
        )
        event_a = await asyncio.wait_for(queue_a.get(), timeout=1.0)
        event_b = await asyncio.wait_for(queue_b.get(), timeout=1.0)
    assert isinstance(event_a, AlertFiredEvent)
    assert isinstance(event_b, AlertFiredEvent)
    assert event_a.alert_id == event_b.alert_id == "alert-2"
