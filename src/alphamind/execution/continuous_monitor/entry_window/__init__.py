"""Entry-window expiry watcher (ALP-737).

Auto-cancels a still-``PENDING_ENTRY`` bracket entry once
``now() > entry_window_deadline`` and the entry has not filled — the
enforcement the ``BracketRecord`` lifecycle contract (``orders.py``) names but
that no producer wired before. Composes existing primitives: a status-keyed
``PENDING_ENTRY`` bracket read, the broker adapter's ``submit_cancel``, and the
Phase-2 CANCEL writeback state machine (``_writeback_cancel``).
"""

from __future__ import annotations

from alphamind.execution.continuous_monitor.entry_window.wiring import (
    register_entry_window_watcher_task,
)

__all__ = ["register_entry_window_watcher_task"]
