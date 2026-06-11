"""Production process controller for a supervised NSSM service.

:class:`NssmServiceController` is the watchdog's restart seam in production: it
shells out to ``nssm restart <service>`` to restart the wedged supervised
service (the safety core, the continuous monitor). It satisfies the
:class:`ProcessController` Protocol the watchdog depends on, so the watchdog's
restart-on-staleness logic stays testable against a fake controller while
production uses NSSM (the Windows service manager that supervises every
AlphaMind service).

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
    """Default runner: ``subprocess.run`` with ``check=True`` (raises on failure).

    Output is captured so the raised :class:`subprocess.CalledProcessError`
    carries the child's stderr — the SCM's refusal reason lives there, not in
    the exit status (ALP-945). ``errors="replace"`` keeps a non-decodable byte
    in that output from raising ``UnicodeDecodeError`` instead of the
    ``CalledProcessError`` the caller logs. On success the captured output is
    discarded (it no longer passes through to the watchdog's own stdio).
    """
    subprocess.run(cmd, check=True, capture_output=True, text=True, errors="replace")


class NssmServiceController:
    """Restarts a supervised NSSM service by shelling out to ``nssm restart``."""

    def __init__(
        self,
        *,
        service_name: str,
        run: RunCommand = _checked_run,
    ) -> None:
        self._service_name = service_name
        self._run = run

    def restart(self) -> None:
        """Restart the supervised service via ``nssm restart``.

        A failed restart shell-out is logged but not re-raised: the watchdog must
        keep probing so a transient ``nssm`` failure on one tick does not kill
        the watchdog itself (the broker floor still protects positions
        regardless).
        """
        cmd: Sequence[str] = ["nssm", "restart", self._service_name]
        log.critical("watchdog: restarting service %r", self._service_name)
        try:
            self._run(list(cmd))
        except subprocess.CalledProcessError as exc:
            # The SCM's refusal reason lives in the child's stderr (ALP-945).
            log.exception(
                "watchdog: nssm restart of %r failed (stderr: %s); will retry next tick",
                self._service_name,
                exc.stderr or "<no stderr captured>",
            )
        except Exception:
            log.exception(
                "watchdog: nssm restart of %r failed; will retry next tick", self._service_name
            )
