"""Notification channels for the alert engine (story 05a / ALP-671).

Two channel kinds in v1:

* :mod:`alphamind.command_center.alerts.channels.in_app` —
  :class:`InAppChannel` pushes a synthetic :class:`MonitorEvent`
  carrying the fired alert onto the per-process
  :class:`EventMultiplexer`. The browser's existing SSE consumer
  picks it up; the alert-banner component renders it.
* :mod:`alphamind.command_center.alerts.channels.discord` —
  :class:`DiscordChannel` Protocol + :class:`RealDiscordChannel`
  (``httpx.AsyncClient``-backed) + :class:`FakeDiscordChannel`
  (in-memory recorder for tests).

The channel registry is structured so a future channel kind (email /
SMS) drops in as a new module without altering the alert-rule
contract.
"""

from __future__ import annotations

from alphamind.command_center.alerts.channels.discord import (
    DiscordChannel,
    FakeDiscordChannel,
    RealDiscordChannel,
)
from alphamind.command_center.alerts.channels.in_app import InAppChannel

__all__ = [
    "DiscordChannel",
    "FakeDiscordChannel",
    "InAppChannel",
    "RealDiscordChannel",
]
