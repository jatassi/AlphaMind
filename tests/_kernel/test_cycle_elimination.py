"""Verify the 10-module import cycle motivating ALP-457 is gone.

Before this story the import graph contained a strongly-connected component
threading ``portfolio_state.aggregates.drawdown -> ... ->
distillation.output -> portfolio_state.aggregates.drawdown`` — ten modules
held together by the four shared enums and the calibration vocabulary.

The cycle-detector script under ``.claude/skills/python-architecture/scripts``
is treated as the source of truth: this test invokes it on
``src/alphamind`` and asserts no remaining cycle still threads the four
modules at the core of the original SCC.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANALYZER = _REPO_ROOT / ".claude/skills/python-architecture/scripts/analyze_imports.py"
_PACKAGE = _REPO_ROOT / "src/alphamind"


def _run_analyzer() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(_ANALYZER), str(_PACKAGE)],
        check=True,
        capture_output=True,
        text=True,
    )
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def test_ten_module_cycle_through_distillation_output_is_gone() -> None:
    """No cycle should contain the four-module spine of the historical SCC.

    The historical 10-module cycle threaded
    ``portfolio_state.aggregates.drawdown`` ↔
    ``portfolio_state.records.capital`` ↔
    ``distillation.calibration`` ↔ ``distillation.output``.
    Any cycle still listing all four indicates the cycle survived the refactor.
    """
    result = _run_analyzer()
    spine = {
        "alphamind.portfolio_state.aggregates.drawdown",
        "alphamind.portfolio_state.records.capital",
        "alphamind.distillation.calibration",
        "alphamind.distillation.output",
    }
    for cycle in result["cycles"]:
        assert not spine.issubset(set(cycle)), (
            f"10-module cycle through {sorted(spine)} still present: {cycle}"
        )
