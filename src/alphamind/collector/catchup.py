"""
Catch-up sweep: calls every registered collector function once with ``since=None``.

Runs sequentially — no APScheduler needed since there is only one in-flight
call at a time.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from alphamind.collector.scheduler import COLLECTORS

log = logging.getLogger(__name__)


def run_all(
    only: Iterable[str] | None = None,
    skip: Iterable[str] | None = None,
) -> None:
    """Invoke each registered collector once with ``since=None``.

    Parameters
    ----------
    only:
        If set, run only these collector IDs (in registry order).
    skip:
        If set, skip these collector IDs.

    A collector ID may be:
    - A full registry key (e.g., ``polygon.equity``).
    - A bare vendor prefix (e.g., ``polygon``) which matches every key
      whose dotted prefix equals the value.
    """
    only_set = set(only) if only else None
    skip_set = set(skip) if skip else set()

    def _match(cid: str, selectors: set[str]) -> bool:
        prefix = cid.split(".", 1)[0]
        return cid in selectors or prefix in selectors

    for collector_id, fn in COLLECTORS.items():
        if only_set is not None and not _match(collector_id, only_set):
            continue
        if _match(collector_id, skip_set):
            log.info("catch-up: collector=%s skipped", collector_id)
            continue
        log.info("catch-up: collector=%s start", collector_id)
        try:
            fn(since=None)
            log.info("catch-up: collector=%s done", collector_id)
        except Exception:
            # Per-collector supervisor per runtime §G1: one collector raising
            # must not abort the catch-up sweep. ``BaseException``
            # (``KeyboardInterrupt``) propagates so the sweep can be cancelled.
            log.exception("catch-up: collector=%s error", collector_id)
