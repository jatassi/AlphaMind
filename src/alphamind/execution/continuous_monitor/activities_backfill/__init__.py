"""Periodic fill-backfill backstop sub-package (ALP-763).

On an interval — independent of any websocket reconnect — the continuous
monitor sweeps Alpaca for fills missing from ``fill_records`` and feeds them
through the normal persist path, then drains the unattributed-fills queue.
This closes the gap the reconnect-driven recovery leaves: a fill dropped /
quarantined before its ``orders`` row committed, which max(fill_timestamp)
recovery permanently excludes once a later fill lands.

Layout mirrors the sibling ``greeks_refresh/`` sub-package:

* :mod:`task` — the run-forever asyncio task (imperative shell), reusing
  ``recover_missed_fills_since`` + the shared ``persist_fill_report`` +
  ``drain_unattributed_fills``.
* :mod:`wiring` — supervisor registration helper.
"""

from alphamind.execution.continuous_monitor.activities_backfill.task import (
    run_fill_backfill,
)
from alphamind.execution.continuous_monitor.activities_backfill.wiring import (
    register_fill_backfill_task,
)

__all__ = [
    "register_fill_backfill_task",
    "run_fill_backfill",
]
