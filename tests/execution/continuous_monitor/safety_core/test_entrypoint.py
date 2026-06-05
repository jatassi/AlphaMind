"""Tests for the safety-core entrypoint dispatch + NSSM controller (ALP-857).

The package is two NSSM services off one module: ``run`` (the safety core) and
``watchdog`` (the dedicated out-of-process watchdog). ``_parse_args`` routes the
subcommand; ``NssmServiceController`` shells out to ``nssm restart`` to restart
the wedged core. The restart shell-out is the OS-process boundary — tested
against an injected ``run`` callable, never a real ``nssm`` invocation.
"""

from __future__ import annotations

import pytest

from alphamind.execution.continuous_monitor.safety_core.__main__ import _parse_args
from alphamind.execution.continuous_monitor.safety_core.process_control import (
    NssmServiceController,
)


def test_parse_args_routes_run_subcommand() -> None:
    args = _parse_args(["run"])
    assert args.subcommand == "run"


def test_parse_args_routes_watchdog_subcommand() -> None:
    args = _parse_args(["watchdog"])
    assert args.subcommand == "watchdog"


def test_parse_args_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        _parse_args([])


def test_nssm_controller_restart_shells_out_to_nssm() -> None:
    """``restart`` invokes ``nssm restart <service>`` via the injected runner."""
    calls: list[list[str]] = []
    controller = NssmServiceController(
        service_name="alphamind-safety-core",
        run=lambda cmd: calls.append(cmd),
    )

    controller.restart()

    assert calls == [["nssm", "restart", "alphamind-safety-core"]]
