"""Discord webhook channel — Protocol + Real + Fake (story 05a / ALP-671).

Three classes:

* :class:`DiscordChannel` — :func:`typing.runtime_checkable` Protocol the
  engine consumes. One async :meth:`send` method.
* :class:`RealDiscordChannel` — ``httpx.AsyncClient``-backed
  implementation. POSTs the Discord-webhook embed payload to the
  configured URL. Webhook URL sourced from the
  ``${ALPHAMIND_DISCORD_WEBHOOK}`` env var per ``config/alerts.yaml``.
  When the env var is unset, :meth:`send` logs WARNING and skips.
* :class:`FakeDiscordChannel` — in-memory recorder for tests. Records
  every ``send`` call's args so route + engine tests can assert the
  fanout happened.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from alphamind.command_center._kernel.ids import (
    AlertId,
    AlertRuleName,
    DiscordWebhookUrl,
)
from alphamind.command_center.persistence.codecs import AlertSeverity

__all__ = [
    "DiscordChannel",
    "FakeDiscordChannel",
    "RealDiscordChannel",
]

log = logging.getLogger(__name__)


_SEVERITY_COLORS: dict[AlertSeverity, int] = {
    AlertSeverity.CRITICAL: 0xE74C3C,  # red
    AlertSeverity.IMPORTANT: 0xE67E22,  # orange
    AlertSeverity.OPERATIONAL: 0x3498DB,  # blue
}
"""Discord embed colors per severity tier.

Decorative only — the body fields carry the authoritative severity
label too. The colors are stock Discord brand-style hex values.
"""


@runtime_checkable
class DiscordChannel(Protocol):
    """Discord-webhook side of the alert channel fanout.

    Returns ``True`` when the webhook accepted the payload, ``False``
    when the channel intentionally skipped (no webhook URL configured)
    or the upstream returned a non-2xx. The engine logs at WARNING
    in both skip cases — the in-app channel still fired so the
    operator never misses a critical alert because the webhook is
    down.
    """

    async def send(
        self,
        *,
        alert_id_: AlertId,
        rule_name: AlertRuleName,
        severity: AlertSeverity,
        title: str,
        context: Mapping[str, Any],
        deep_link_url: str,
    ) -> bool: ...


@dataclass
class RealDiscordChannel:
    """Production :class:`DiscordChannel` against ``${ALPHAMIND_DISCORD_WEBHOOK}``.

    ``webhook_url`` is the parsed-validated URL; ``http_client`` is the
    shared ``httpx.AsyncClient`` the composition root threads in (same
    pattern as the SSE events client — F10 single connection pool). When
    ``webhook_url`` is ``None``, :meth:`send` logs WARNING and returns
    ``False`` so the engine continues to the next channel.
    """

    webhook_url: DiscordWebhookUrl | None
    http_client: httpx.AsyncClient

    async def send(
        self,
        *,
        alert_id_: AlertId,
        rule_name: AlertRuleName,
        severity: AlertSeverity,
        title: str,
        context: Mapping[str, Any],
        deep_link_url: str,
    ) -> bool:
        if self.webhook_url is None:
            log.warning(
                "discord channel: ALPHAMIND_DISCORD_WEBHOOK not configured; "
                "skipping (rule=%s severity=%s)",
                str(rule_name),
                severity.value,
            )
            return False
        embed = {
            "title": title,
            "description": _format_description(rule_name, severity, context),
            "color": _SEVERITY_COLORS.get(severity, 0x95A5A6),
            "url": deep_link_url,
            "fields": [
                {"name": "Rule", "value": str(rule_name), "inline": True},
                {"name": "Severity", "value": severity.value, "inline": True},
                {"name": "Alert id", "value": str(alert_id_), "inline": False},
            ],
        }
        payload = {"embeds": [embed]}
        try:
            response = await self.http_client.post(self.webhook_url, json=payload)
        except httpx.HTTPError as exc:
            log.warning(
                "discord channel: post failed (rule=%s, err=%s)",
                str(rule_name),
                exc,
            )
            return False
        if response.status_code >= 400:
            log.warning(
                "discord channel: non-2xx response (rule=%s, status=%d, body=%r)",
                str(rule_name),
                response.status_code,
                response.text[:200],
            )
            return False
        return True


def _format_description(
    rule_name: AlertRuleName,
    severity: AlertSeverity,
    context: Mapping[str, Any],
) -> str:
    """Render the context mapping as a short readable description.

    Discord's ``description`` field renders Markdown; we keep the body
    a compact list of ``key: value`` pairs sorted alphabetically so the
    rendering stable across context payload variations.
    """
    if not context:
        return f"Alert fired for {rule_name!s} ({severity.value})."
    lines = [f"**{rule_name!s}** ({severity.value}):"]
    for key in sorted(context):
        lines.append(f"- `{key}`: `{context[key]}`")
    return "\n".join(lines)


@dataclass
class FakeDiscordChannel:
    """In-memory :class:`DiscordChannel` for tests.

    Records every ``send`` call's args under :attr:`calls` so the test
    asserting on fan-out reads them back deterministically. The
    ``return_value`` field lets a test pin the channel's return code
    (e.g. simulate a 5xx by returning False).
    """

    calls: list[Mapping[str, Any]] = field(default_factory=list)
    return_value: bool = True

    async def send(
        self,
        *,
        alert_id_: AlertId,
        rule_name: AlertRuleName,
        severity: AlertSeverity,
        title: str,
        context: Mapping[str, Any],
        deep_link_url: str,
    ) -> bool:
        self.calls.append(
            {
                "alert_id_": alert_id_,
                "rule_name": rule_name,
                "severity": severity,
                "title": title,
                "context": dict(context),
                "deep_link_url": deep_link_url,
            }
        )
        return self.return_value
