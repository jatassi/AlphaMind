"""Daily borrow-accrual sub-package (ALP-719 / parent ALP-715).

Once per trading day, the continuous monitor advances every OPEN
SHORT-equity position's ``EquityPositionDetails.accrued_borrow_cost_usd``
accumulator by one day's worth of borrow cost and emits a
``BORROW_COST_ACCRUED`` activity-log entry per position.

Layout mirrors the sibling ``greeks_refresh/`` sub-package:

* :mod:`recompute` — pure tick kernel (functional core).
* :mod:`task` — long-running asyncio task (imperative shell).
* :mod:`wiring` — supervisor registration helpers.

See :doc:`docs/design/05-execution-layer/architecture.md` § 4f and
:doc:`docs/design/05-execution-layer/short-equity-write-path.md`
§ Daily accrual tick.
"""

from alphamind.execution.continuous_monitor.borrow_accrual.recompute import (
    AccrualTickResult,
    compute_tick,
)
from alphamind.execution.continuous_monitor.borrow_accrual.task import (
    BorrowCostResolverFactory,
    TradingCalendar,
    run_accrual_tick,
    run_borrow_accrual_loop,
)
from alphamind.execution.continuous_monitor.borrow_accrual.wiring import (
    register_borrow_accrual_task,
)

__all__ = [
    "AccrualTickResult",
    "BorrowCostResolverFactory",
    "TradingCalendar",
    "compute_tick",
    "register_borrow_accrual_task",
    "run_accrual_tick",
    "run_borrow_accrual_loop",
]
