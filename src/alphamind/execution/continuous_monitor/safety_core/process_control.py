"""Production process controller for the safety core (ALP-857 / ADR-0004).

:class:`NssmServiceController` is the watchdog's restart seam in production: it
shells out to ``nssm restart alphamind-safety-core`` to restart the wedged
safety-core service. It satisfies the :class:`ProcessController` Protocol the
watchdog depends on, so the watchdog's restart-on-staleness logic stays testable
against a fake controller while production uses NSSM (the Windows service
manager that supervises every AlphaMind service).

The ``run`` shell-out callable is injected so the boundary is faked in tests; it
defaults to a checked ``subprocess.run``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence

log = logging.getLogger(__name__)

RunCommand = Callable[[list[str]], object]


def _checked_run(cmd: list[str]) -> None:
    """Default runner: ``subprocess.run`` with ``check=True`` (raises on failure)."""
    subprocess.run(cmd, check=True)


class NssmServiceController:
    """Restarts the safety-core NSSM service by shelling out to ``nssm restart``."""

    def __init__(
        self,
        *,
        service_name: str = "alphamind-safety-core",
        run: RunCommand = _checked_run,
    ) -> None:
        self._service_name = service_name
        self._run = run

    def restart(self) -> None:
        """Restart the supervised safety-core service via ``nssm restart``.

        A failed restart shell-out is logged but not re-raised: the watchdog must
        keep probing so a transient ``nssm`` failure on one tick does not kill
        the watchdog itself (the broker floor still protects positions
        regardless).
        """
        cmd: Sequence[str] = ["nssm", "restart", self._service_name]
        log.critical("safety-core watchdog: restarting service %r", self._service_name)
        try:
            self._run(list(cmd))
        except Exception:
            log.exception("safety-core watchdog: nssm restart failed; will retry next tick")
