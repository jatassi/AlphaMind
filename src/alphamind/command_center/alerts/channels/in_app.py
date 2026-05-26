"""In-app channel — publishes :class:`AlertFiredEvent` onto the multiplexer.

The browser's existing SSE consumer (story 04b's
:func:`build_events_router`) picks up the cc-sourced frame and the
alert-banner component renders it. The browser also fetches the active
alerts on page load via ``GET /api/alerts``; the in-app channel just
keeps the SSE stream fresh without requiring a page reload.

The channel is a tiny imperative shell over the multiplexer: it owns
no state, no debounce, no persistence. The engine has already done the
debouncing + persistence before invoking the channel.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from alphamind.command_center._kernel.events import AlertFiredEvent
from alphamind.command_center._kernel.ids import AlertId, AlertRuleName
from alphamind.command_center.events.multiplexer import EventMultiplexer
from alphamind.command_center.persistence.codecs import AlertSeverity

__all__ = ["InAppChannel"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InAppChannel:
    """Pushes alert-fired events onto the :class:`EventMultiplexer`.

    Construction: the engine threads the per-process multiplexer in.
    Production callers wire one of these alongside the
    :class:`RealDiscordChannel`; tests construct one against a fresh
    multiplexer + subscribe to verify the event lands.
    """

    multiplexer: EventMultiplexer

    async def send(
        self,
        *,
        alert_id_: AlertId,
        rule_name: AlertRuleName,
        severity: AlertSeverity,
        context: Mapping[str, Any],
    ) -> None:
        """Publish the fired alert onto the multiplexer."""
        log.info(
            "in_app channel: publishing alert_fired (rule=%s severity=%s alert_id=%s)",
            str(rule_name),
            severity.value,
            str(alert_id_),
        )
        event = AlertFiredEvent(
            alert_id=str(alert_id_),
            rule_name=str(rule_name),
            severity=severity.value,
            payload=dict(context),
        )
        await self.multiplexer.publish(event)
