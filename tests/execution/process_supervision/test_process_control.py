"""Tests for the NSSM process controller (ALP-857 / ALP-941).

``NssmServiceController`` is the watchdog's restart seam in production: it
shells out to ``nssm restart <service>`` to restart the wedged supervised
service. The restart shell-out is the OS-process boundary — tested against an
injected ``run`` callable, never a real ``nssm`` invocation.
"""

from __future__ import annotations

from alphamind.execution.process_supervision.process_control import (
    NssmServiceController,
)


def test_nssm_controller_restart_shells_out_to_nssm() -> None:
    """``restart`` invokes ``nssm restart <service>`` via the injected runner."""
    calls: list[list[str]] = []
    controller = NssmServiceController(
        service_name="alphamind-safety-core",
        run=lambda cmd: calls.append(cmd),
    )

    controller.restart()

    assert calls == [["nssm", "restart", "alphamind-safety-core"]]


def test_nssm_controller_swallows_a_failed_shell_out() -> None:
    """A failed ``nssm restart`` is logged, not re-raised — the watchdog keeps probing."""

    def _boom(cmd: list[str]) -> None:
        del cmd
        raise RuntimeError("nssm unavailable")

    controller = NssmServiceController(service_name="alphamind-monitor", run=_boom)

    controller.restart()  # must not raise
