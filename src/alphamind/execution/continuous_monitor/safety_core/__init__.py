"""The isolated safety core + its dedicated out-of-process watchdog (ALP-857).

Per ADR-0004, the safety core is the lone safety item with **no broker floor** —
portfolio breach detection + underlying price-staleness — extracted into its own
minimal, non-blocking process. It reads positions from the **broker snapshot**
(the Broker-Owned Fact) and prices from the **live underlying stream**, never the
DB fill-projection, and **writes nothing** to the shared DB (it emits a heartbeat
to a non-DB file sink). It imports neither pipeline nor monitor internals (an
``.importlinter`` contract enforces the boundary).

A **dedicated out-of-process watchdog** probes the safety core's file heartbeat
and restarts it on staleness. It is a separate process, not a loop-resident
probe: a loop-resident watchdog cannot catch a freeze of its own loop — the
structural ALP-841 failure this isolation makes unrepresentable.

Public surface:

* :func:`evaluate_safety` — the pure evaluation core.
* :class:`SafetyEvaluation`, :class:`SafetyPosition`, :class:`PriceStaleness`,
  :class:`SafetyLimits` — the typed records.
* :func:`run_safety_core` — the imperative shell loop.
* :func:`run_watchdog` — the out-of-process watchdog loop.
* :class:`FileHeartbeatSink`, :class:`FileHeartbeatProbe` — the non-DB heartbeat.
"""

from alphamind.execution.continuous_monitor.safety_core.evaluation import (
    SafetyLimits,
    evaluate_safety,
)
from alphamind.execution.continuous_monitor.safety_core.heartbeat import (
    FileHeartbeatProbe,
    FileHeartbeatSink,
)
from alphamind.execution.continuous_monitor.safety_core.loop import run_safety_core
from alphamind.execution.continuous_monitor.safety_core.records import (
    PriceStaleness,
    SafetyEvaluation,
    SafetyPosition,
)
from alphamind.execution.continuous_monitor.safety_core.watchdog import (
    ProcessController,
    run_watchdog,
)

__all__ = [
    "FileHeartbeatProbe",
    "FileHeartbeatSink",
    "PriceStaleness",
    "ProcessController",
    "SafetyEvaluation",
    "SafetyLimits",
    "SafetyPosition",
    "evaluate_safety",
    "run_safety_core",
    "run_watchdog",
]
