"""Entry-window expiry watcher (ALP-737 + ALP-740).

Acts on a still-``PENDING_ENTRY`` bracket entry once
``now() > entry_window_deadline`` and the entry has not filled — the
enforcement the ``BracketRecord`` lifecycle contract (``orders.py``) names but
that no producer wired before. ALP-740 first reprices/escalates the resting
limit toward the market (bounded by ``entry_window_max_reprices``) so an
accepted thesis still gets a fill, falling back to the terminal cancel once the
budget is spent. Composes existing primitives: a status-keyed ``PENDING_ENTRY``
bracket read, the broker adapter's ``submit_replace`` / ``submit_cancel`` and
``marketable_limit_price``, and the Phase-2 reprice / CANCEL writebacks.
"""

from __future__ import annotations

from alphamind.execution.continuous_monitor.entry_window.wiring import (
    register_entry_window_watcher_task,
)

__all__ = ["register_entry_window_watcher_task"]
