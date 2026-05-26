"""Tests for :mod:`alphamind.command_center.alerts.channels.discord` (story 05a / ALP-671)."""

from __future__ import annotations

import logging

import httpx
import pytest

from alphamind.command_center._kernel.ids import (
    alert_id,
    alert_rule_name,
    discord_webhook_url,
)
from alphamind.command_center.alerts.channels.discord import (
    DiscordChannel,
    FakeDiscordChannel,
    RealDiscordChannel,
)
from alphamind.command_center.persistence.codecs import AlertSeverity


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeDiscordChannel(), DiscordChannel)


@pytest.mark.asyncio
async def test_fake_records_calls() -> None:
    fake = FakeDiscordChannel()
    ok = await fake.send(
        alert_id_=alert_id("alert-1"),
        rule_name=alert_rule_name("test_rule"),
        severity=AlertSeverity.CRITICAL,
        title="Critical alert",
        context={"foo": "bar"},
        deep_link_url="https://commandcenter.example/alerts/alert-1",
    )
    assert ok is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["rule_name"] == alert_rule_name("test_rule")
    assert fake.calls[0]["title"] == "Critical alert"


@pytest.mark.asyncio
async def test_fake_returns_configured_return_value() -> None:
    fake = FakeDiscordChannel(return_value=False)
    ok = await fake.send(
        alert_id_=alert_id("alert-2"),
        rule_name=alert_rule_name("test_rule"),
        severity=AlertSeverity.IMPORTANT,
        title="t",
        context={},
        deep_link_url="https://commandcenter.example/alerts/alert-2",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_real_with_unset_webhook_logs_warning_and_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        channel = RealDiscordChannel(webhook_url=None, http_client=client)
        with caplog.at_level(
            logging.WARNING,
            logger="alphamind.command_center.alerts.channels.discord",
        ):
            ok = await channel.send(
                alert_id_=alert_id("alert-3"),
                rule_name=alert_rule_name("test_rule"),
                severity=AlertSeverity.CRITICAL,
                title="Critical alert",
                context={"foo": "bar"},
                deep_link_url="https://example.com/alerts/alert-3",
            )
    assert ok is False
    assert any("not configured" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_real_posts_embed_on_success() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "url": str(request.url),
                "method": request.method,
                "body": request.read().decode(),
            }
        )
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        channel = RealDiscordChannel(
            webhook_url=discord_webhook_url(
                "https://discord.com/api/webhooks/123/abc-def_token-123"
            ),
            http_client=client,
        )
        ok = await channel.send(
            alert_id_=alert_id("alert-4"),
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            title="Critical alert",
            context={"foo": "bar"},
            deep_link_url="https://example.com/alerts/alert-4",
        )
    assert ok is True
    assert len(captured) == 1
    assert captured[0]["method"] == "POST"
    assert "discord.com" in str(captured[0]["url"])
    body = str(captured[0]["body"])
    assert "embeds" in body
    assert "test_rule" in body
    assert "alert-4" in body


@pytest.mark.asyncio
async def test_real_returns_false_on_5xx() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as client:
        channel = RealDiscordChannel(
            webhook_url=discord_webhook_url(
                "https://discord.com/api/webhooks/123/abc-def_token-123"
            ),
            http_client=client,
        )
        ok = await channel.send(
            alert_id_=alert_id("alert-5"),
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            title="t",
            context={},
            deep_link_url="https://example.com/alerts/alert-5",
        )
    assert ok is False


@pytest.mark.asyncio
async def test_real_returns_false_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        channel = RealDiscordChannel(
            webhook_url=discord_webhook_url(
                "https://discord.com/api/webhooks/123/abc-def_token-123"
            ),
            http_client=client,
        )
        ok = await channel.send(
            alert_id_=alert_id("alert-6"),
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            title="t",
            context={},
            deep_link_url="https://example.com/alerts/alert-6",
        )
    assert ok is False
