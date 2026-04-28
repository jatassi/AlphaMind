"""Verify `python -m alphamind.distillation.replay_harness` invocation.

Confirms `__main__.py` wires `cli.main()` through `sys.exit` and the help
output renders without crashing.
"""

from __future__ import annotations

import subprocess
import sys

from alphamind.distillation.replay_harness.cli import (
    NOT_YET_IMPLEMENTED_EXIT_CODE,
    NOT_YET_IMPLEMENTED_MESSAGE,
)

_MODULE = "alphamind.distillation.replay_harness"


def test_help_flag_renders_and_lists_all_arguments() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", _MODULE, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--candidate-config" in completed.stdout
    assert "--baseline-config" in completed.stdout
    assert "--regimes" in completed.stdout


def test_no_args_exits_nonzero_with_missing_required_message() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", _MODULE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "--candidate-config" in completed.stderr


def test_module_invocation_with_candidate_config_returns_sentinel() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", _MODULE, "--candidate-config", "/nonexistent.yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == NOT_YET_IMPLEMENTED_EXIT_CODE
    assert NOT_YET_IMPLEMENTED_MESSAGE in completed.stderr
