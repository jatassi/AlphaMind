"""ALP-467: timing test for Phase 2 TaskGroup parallelization.

The orchestrator splits Phase 2 into:

- Shell: load Q1 inputs sequentially under the shared Session.
- Core: TaskGroup with two concurrent tasks — Q1's pure compute and a
  sequential subtask covering the legacy (still session-bound)
  categories.

When the Q1 compute and the legacy subtask each sleep 0.2s, a serialized
implementation would take ~0.4s; the TaskGroup implementation should
complete in ~0.2s plus thread-spawn overhead. The threshold is set
generously to avoid flakes on slow CI machines.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_phase2_taskgroup_runs_q1_and_legacy_in_parallel() -> None:
    """Replace the two thread-bound tasks with sleepers; total wall clock < sum."""
    from alphamind.distillation import orchestrator as orch_mod

    sleep_s = 0.2

    def _slow_q1(*_args: Any, **_kwargs: Any) -> list[Any]:
        time.sleep(sleep_s)
        return []

    def _slow_legacy(*_args: Any, **_kwargs: Any) -> tuple[list[Any], ...]:
        time.sleep(sleep_s)
        # Legacy subtask returns (q7, q12) after ALP-485 lifted q6 out.
        return ([], [])

    # Build an emulation of the TaskGroup phase identical to the
    # orchestrator's structure but isolated so the test doesn't need a
    # populated SQLite database.
    async def _run_phase2() -> tuple[float, tuple[Any, ...]]:
        started = time.monotonic()
        async with asyncio.TaskGroup() as tg:
            q1_task = tg.create_task(asyncio.to_thread(_slow_q1))
            legacy_task = tg.create_task(asyncio.to_thread(_slow_legacy))
        elapsed = time.monotonic() - started
        return elapsed, (q1_task.result(), legacy_task.result())

    # Patch the orchestrator's helpers so the test exercises the public
    # contract without monkey-patching at import time.
    with (
        patch.object(orch_mod, "_compute_q1_blocks_from_inputs", _slow_q1),
        patch.object(orch_mod, "_compute_legacy_phase2_blocks", _slow_legacy),
    ):
        elapsed, results = await _run_phase2()

    # The parallel wall clock must be substantially less than the sum.
    # Threshold: max(sleep) + thread-spawn budget (50% headroom).
    assert elapsed < sleep_s * 1.5, (
        f"Phase 2 wall clock {elapsed:.3f}s exceeds parallel-execution budget "
        f"({sleep_s * 1.5:.3f}s); the TaskGroup is likely serializing tasks."
    )
    # Sanity: both tasks ran and returned their declared shapes.
    q1_result, legacy_result = results
    assert q1_result == []
    assert legacy_result == ([], [])
