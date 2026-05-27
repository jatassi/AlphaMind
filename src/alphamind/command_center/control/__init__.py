"""Browser-facing ``/api/control/*`` proxy + audit (story 04a / ALP-668).

Surface shape (one module per concern):

* :mod:`alphamind.command_center.control.models` — Pydantic request /
  response / error envelopes for the eight ``/api/control/*`` POST
  endpoints. Mirrors the upstream pipeline + monitor schemas.
* :mod:`alphamind.command_center.control.pipeline_client` —
  :class:`PipelineClient` Protocol + ``Real`` / ``Fake`` implementations
  for the five pipeline verbs (``pause`` / ``resume`` /
  ``trigger_emergency_invocation`` / ``switch_profile`` /
  ``run_universe_validation``).
* :mod:`alphamind.command_center.control.monitor_client` —
  :class:`MonitorClient` Protocol + ``Real`` / ``Fake`` implementations
  for the three monitor verbs (``cancel_order`` /
  ``force_close_position`` / ``set_halt_mode``).
* :mod:`alphamind.command_center.control.audit` — typed activity-log
  writer the proxy invokes inside the open
  :func:`~alphamind.command_center._kernel.operator_invocation.operator_invocation`
  handle.
* :mod:`alphamind.command_center.control.proxy` — eight async
  ``proxy_*`` functions, one per verb, that open the operator-invocation
  handle, dispatch to the client, write the audit row, and return the
  typed :class:`~alphamind.command_center._kernel.control.ControlResult`.
* :mod:`alphamind.command_center.control.routes` — eight FastAPI POST
  handlers under ``/api/control/*``. Each handler depends on
  ``current_session`` and ``csrf_required``, validates the Pydantic
  request body, dispatches to the matching ``proxy_*`` function, and
  maps the ``ControlResult`` back onto the upstream-shaped Pydantic
  response or error envelope.

Per the ALP-128 architectural invariants:

* **Pydantic at boundaries only.** ``models.py`` is the only module that
  consumes :class:`pydantic.BaseModel`; ``proxy.py`` /
  ``pipeline_client.py`` / ``monitor_client.py`` operate on frozen
  dataclasses.
* **No direct cross-process imports.** This subpackage does NOT import
  from ``alphamind.scheduler`` or
  ``alphamind.execution.continuous_monitor``; all cross-process traffic
  flows over loopback HTTP via :class:`httpx.AsyncClient`.
* **Protocols + in-memory fakes for cross-process clients.** Every test
  exercises the proxy and routes against fake clients; real httpx calls
  are exercised only by the story-07 verify script.
"""

from __future__ import annotations
