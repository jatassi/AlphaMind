"""Alert engine subpackage (ALP-671, story 05a).

Watches the multiplexed event stream + a 60s periodic timer, evaluates
the 17 default rules from :doc:`docs/design/command-center.md`
§ Alerting against state transitions, debounces, routes by severity to
the in-app banner + Discord webhook, and exposes acknowledge / snooze
APIs.

The package is a deep module per the python-architecture P9 principle:
``engine.py`` is the single class that hides condition evaluation,
debounce, severity routing, and channel fanout. The supporting modules
expose typed value objects + the per-rule predicates, but downstream
consumers only construct one :class:`AlertEngine` and call
:meth:`AlertEngine.run` on the supervisor's TaskGroup.
"""

from __future__ import annotations
