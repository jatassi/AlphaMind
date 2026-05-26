"""View-assembly routers for the command center (story 05b / ALP-672).

Each view router exposes one-shot snapshot endpoints that assemble the
data the frontend pane needs on connect / reconnect. Live updates arrive
via the SSE multiplexer (``/api/events``); the view endpoints are the
fallback / initial-load path.

Sub-modules:

* :mod:`alphamind.command_center.views.live_operations` — ``GET /api/views/live``
  and ``GET /api/views/schedule``.
"""
