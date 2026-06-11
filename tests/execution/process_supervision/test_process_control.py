"""Tests for the NSSM process controller (ALP-857 / ALP-941).

``NssmServiceController`` is the watchdog's restart seam in production: it
shells out to ``nssm restart <service>`` to restart the wedged supervised
service. The restart shell-out is the OS-process boundary — tested against an
injected ``run`` callable, never a real ``nssm`` invocation.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from alphamind.execution.process_supervision.process_control import (
    NssmServiceController,
    _checked_run,
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


def test_nssm_controller_failure_log_carries_the_shell_outs_stderr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``CalledProcessError``'s stderr reaches the failure log line.

    The 2026-06-10 incident (ALP-945): sixteen refused restarts logged only
    "exit status 1" while the SCM's refusal reason sat in the child's stderr.
    The log line must carry that reason — and the error is still swallowed so
    the watchdog keeps probing.
    """
    refusal = (
        "alphamind-monitor: STOP: A stop control has been sent to a service "
        "that other running services are dependent on."
    )

    def _refused(cmd: list[str]) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr=refusal)

    controller = NssmServiceController(service_name="alphamind-monitor", run=_refused)

    with caplog.at_level(
        logging.ERROR, logger="alphamind.execution.process_supervision.process_control"
    ):
        controller.restart()  # must not raise

    assert refusal in caplog.text


def test_checked_run_attaches_the_childs_stderr_to_the_raised_error() -> None:
    """The default runner captures the child's stderr onto ``CalledProcessError``.

    Exercised against a real subprocess — the runner IS the OS-process
    boundary, so there is nothing to fake below it. The child's stderr
    includes a byte no text encoding need accept: a non-decodable byte must
    degrade to a replacement character, not raise ``UnicodeDecodeError`` in
    place of the ``CalledProcessError`` the watchdog logs.
    """
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.buffer.write(b'stop control refused \\xff'); sys.exit(1)",
    ]

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _checked_run(cmd)

    assert "stop control refused" in excinfo.value.stderr
