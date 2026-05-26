"""View-assembly routers for the command center (stories 05b-05j, 06a-06c).

Each view router exposes one-shot snapshot endpoints that assemble the
data the frontend pane needs on connect / reconnect. Live updates arrive
via the SSE multiplexer (``/api/events``); the view endpoints are the
fallback / initial-load path.

Sub-modules:

* :mod:`alphamind.command_center.views.live_operations` — ``GET /api/views/live``
  and ``GET /api/views/schedule`` (story 05b / ALP-672).
* :mod:`alphamind.command_center.views.history` — ``GET /api/views/history/runs``
  + failure-log preset (story 05c / ALP-673).
"""

from __future__ import annotations
