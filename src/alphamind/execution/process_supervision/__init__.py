"""Generic out-of-process supervision primitives (ALP-857 / ALP-941, ADR-0004).

The file-heartbeat + out-of-process-watchdog pattern that makes "the watchdog
dies with the loop it guards" unrepresentable: the supervised process writes a
wall-clock beat to a file (:class:`FileHeartbeatSink`); a **separate** watchdog
process probes the file (:class:`FileHeartbeatProbe` / :func:`run_watchdog`)
and restarts the supervised NSSM service (:class:`NssmServiceController`) when
the beat goes stale. A loop-resident watchdog cannot catch a freeze of its own
loop — the structural ALP-841 failure both the safety core (ALP-857) and the
continuous monitor (ALP-941) escape through this package.

Neutral home: a sibling of ``continuous_monitor``, owned by neither supervised
process, importing no monitor or safety-core internals.
"""

from alphamind.execution.process_supervision.heartbeat import (
    FileHeartbeatProbe,
    FileHeartbeatSink,
    HeartbeatSink,
)
from alphamind.execution.process_supervision.process_control import (
    NssmServiceController,
)
from alphamind.execution.process_supervision.watchdog import (
    HeartbeatProbe,
    ProcessController,
    WatchdogLoop,
    run_watchdog,
    supervised_watchdog_loop,
)

__all__ = [
    "FileHeartbeatProbe",
    "FileHeartbeatSink",
    "HeartbeatProbe",
    "HeartbeatSink",
    "NssmServiceController",
    "ProcessController",
    "WatchdogLoop",
    "run_watchdog",
    "supervised_watchdog_loop",
]
