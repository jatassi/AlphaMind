"""Tests for the safety-core entrypoint dispatch (ALP-857).

The package is two NSSM services off one module: ``run`` (the safety core) and
``watchdog`` (the dedicated out-of-process watchdog). ``_parse_args`` routes the
subcommand. The NSSM controller's own tests live with the shared primitive at
``tests/execution/process_supervision/test_process_control.py`` (ALP-941 hoist).
"""

from __future__ import annotations

import pytest

from alphamind.execution.continuous_monitor.safety_core.__main__ import (
    _log_filename_for_subcommand,
    _parse_args,
)


def test_parse_args_routes_run_subcommand() -> None:
    args = _parse_args(["run"])
    assert args.subcommand == "run"


def test_run_and_watchdog_log_to_distinct_non_monitor_files() -> None:
    """Each safety-core process gets its own rotating file (ALP-868).

    The safety core, its watchdog, and the continuous monitor are three separate
    processes; on Windows ``TimedRotatingFileHandler`` cannot rotate a file held
    open by another process, so no two may share a filename.
    """
    run_log = _log_filename_for_subcommand("run")
    watchdog_log = _log_filename_for_subcommand("watchdog")

    assert run_log == "safety_core.log"
    assert watchdog_log == "safety_core_watchdog.log"
    assert len({run_log, watchdog_log, "monitor.log"}) == 3


def test_parse_args_routes_watchdog_subcommand() -> None:
    args = _parse_args(["watchdog"])
    assert args.subcommand == "watchdog"


def test_parse_args_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        _parse_args([])
