"""Loopback-bound FastAPI + Uvicorn HTTP surface for the continuous monitor (ALP-665).

Exposes the three ``POST /control/*`` verbs (cancel_order, force_close_position,
set_halt_mode) and the ``GET /events`` SSE stream specified in
``docs/design/monitor-control-and-events-schema.md``. The surface is the producer
side of the command center's ``/api/control/*`` proxy (story 04a) and
``/api/events`` multiplexer (story 04b); loopback isolation (``127.0.0.1``) is
the trust boundary.
"""

from __future__ import annotations
