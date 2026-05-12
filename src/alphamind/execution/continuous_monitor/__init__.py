"""AlphaMind continuous-monitor package (story 01 — ALP-432).

Public surface re-exported here mirrors what the story acceptance criteria
pin: ``MonitorSession`` + ``new_session`` for per-process handles,
``MonitorSupervisor`` for asyncio task supervision, and
``configure_monitor_logging`` for the standard log file shape. See
``docs/design/05-execution-layer/continuous-monitor-runtime.md`` and
``docs/design/05-execution-layer/architecture.md`` § 4 Continuous monitor for
the surrounding design contract.
"""

from alphamind.execution.continuous_monitor.logging_setup import (
    configure_monitor_logging,
)
from alphamind.execution.continuous_monitor.session import (
    MonitorMode,
    MonitorSession,
    new_session,
)
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor

__all__ = [
    "MonitorMode",
    "MonitorSession",
    "MonitorSupervisor",
    "configure_monitor_logging",
    "new_session",
]
