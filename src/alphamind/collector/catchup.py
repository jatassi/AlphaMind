"""
Catch-up sweep: calls every registered collector function once with ``since=None``.

Runs sequentially — no APScheduler needed since there is only one in-flight
call at a time.
"""

from __future__ import annotations

import logging

from alphamind.collector.scheduler import COLLECTORS

log = logging.getLogger(__name__)


def run_all() -> None:
    """Invoke every collector function with ``since=None`` in registry order."""
    for collector_id, fn in COLLECTORS.items():
        log.info("catch-up: collector=%s start", collector_id)
        try:
            fn(since=None)
            log.info("catch-up: collector=%s done", collector_id)
        except Exception:
            log.exception("catch-up: collector=%s error", collector_id)
