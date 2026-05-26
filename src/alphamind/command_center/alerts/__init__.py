"""Alert engine + channels + APIs for the command center (story 05a / ALP-671).

Subpackage layout:

* :mod:`alphamind.command_center.alerts.rules` —
  :class:`AlertRule`, :class:`AlertCondition` Protocol,
  :class:`AlertSeverity` re-export, :class:`AlertConditionResult`,
  :class:`AlertEvaluatorState`.
* :mod:`alphamind.command_center.alerts.conditions` — 17 default
  condition predicates per the design doc's table, plus the
  :func:`build_default_rules` factory the YAML loader binds to.
* :mod:`alphamind.command_center.alerts.engine` — :class:`AlertEngine`
  long-running task: subscribes to the multiplexer + runs the 60s
  state-polling timer; debounces per ``(rule_name, debounce_key)``;
  fans fires to the configured channels + persists the row.
* :mod:`alphamind.command_center.alerts.persistence` — CRUD against
  the ``alerts`` table: ``insert_fired`` /
  ``update_acknowledged`` / ``update_snoozed`` / ``list_active``.
* :mod:`alphamind.command_center.alerts.channels.in_app` —
  :class:`InAppChannel`: pushes a fired alert onto the multiplexer
  so the browser's SSE consumer renders the banner.
* :mod:`alphamind.command_center.alerts.channels.discord` —
  :class:`DiscordChannel` Protocol + :class:`RealDiscordChannel`
  (httpx-backed) + :class:`FakeDiscordChannel` (test recorder).
* :mod:`alphamind.command_center.alerts.routes` — ``GET /api/alerts``
  + ``POST /api/alerts/{id}/acknowledge`` + ``POST /api/alerts/{id}/snooze``.

Per ALP-128 invariants:

* Pydantic at boundaries only — :mod:`.routes` carries the request /
  response models; the engine + condition predicates + persistence are
  pure frozen dataclasses.
* TaskGroup discipline — :class:`AlertEngine.run` registers on the
  supervisor's outer TaskGroup; no bare ``asyncio.create_task`` calls.
* Protocols + in-memory fakes — :class:`DiscordChannel` is a Protocol
  with a real httpx-backed impl + a fake test recorder.
"""

from __future__ import annotations
