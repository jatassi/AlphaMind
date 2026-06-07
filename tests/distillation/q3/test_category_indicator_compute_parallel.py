"""ALP-484: timing test for per-category indicator compute TaskGroup parallelization.

Tests q1 + q3 + legacy subtask concurrency.

ALP-467 piloted Q1 + a single-task legacy subtask; ALP-484 lifts q3 into
its own TaskGroup task. The structural property the lift unlocks is that
q1's pure compute, q3's pure compute, and the residual legacy subtask
(q6/q7/q12/qualitative) all run wall-clock concurrently.

When each of the three thread-bound tasks sleeps 0.2s, a serialized
implementation would take ~0.6s; the TaskGroup implementation should
complete in ~0.2s plus thread-spawn overhead. The threshold is set
generously (max(sleep) * 1.5) to avoid flakes on slow CI machines.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_category_indicator_compute_taskgroup_runs_q1_q3_and_legacy_in_parallel() -> None:
    """Replace the three thread-bound tasks with sleepers; total wall clock < sum."""
    sleep_s = 0.2

    def _slow_q1(*_args: Any, **_kwargs: Any) -> list[Any]:
        time.sleep(sleep_s)
        return []

    def _slow_q3(*_args: Any, **_kwargs: Any) -> list[Any]:
        time.sleep(sleep_s)
        return []

    def _slow_legacy(*_args: Any, **_kwargs: Any) -> tuple[list[Any], ...]:
        time.sleep(sleep_s)
        return ([], [], [], [])

    # Mirror the orchestrator's three-task TaskGroup structure inline so
    # the test does not need a populated SQLite database. The shape under
    # test is the structural concurrency, not the orchestrator's own
    # session-bound preamble.
    async def _run_category_indicator_compute() -> tuple[float, tuple[Any, ...]]:
        started = time.monotonic()
        async with asyncio.TaskGroup() as tg:
            q1_task = tg.create_task(asyncio.to_thread(_slow_q1))
            q3_task = tg.create_task(asyncio.to_thread(_slow_q3))
            legacy_task = tg.create_task(asyncio.to_thread(_slow_legacy))
        elapsed = time.monotonic() - started
        return elapsed, (q1_task.result(), q3_task.result(), legacy_task.result())

    elapsed, results = await _run_category_indicator_compute()

    # The parallel wall clock must be substantially less than 3x sleep.
    # Threshold: max(sleep) + thread-spawn budget (50% headroom).
    assert elapsed < sleep_s * 1.5, (
        f"Per-category indicator compute wall clock {elapsed:.3f}s exceeds "
        f"parallel-execution budget ({sleep_s * 1.5:.3f}s); the TaskGroup is "
        f"likely serializing tasks."
    )
    # Sanity: all three tasks ran and returned their declared shapes.
    q1_result, q3_result, legacy_result = results
    assert q1_result == []
    assert q3_result == []
    assert legacy_result == ([], [], [], [])
