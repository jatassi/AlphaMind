"""Loop-freeze simulation — the ALP-941 core regression guard.

The 2026-06-09 production wedge: the monitor's event loop froze (a synchronous
block on the loop thread), so the in-loop ``_watchdog_loop`` — whose own
``await sleep`` never resumed — could never reach its ``os._exit(1)``, and no
external watchdog existed to restart the process. This test encodes the fix's
load-bearing property end to end against a REAL frozen event loop:

* the loop thread is genuinely blocked (a synchronous ``threading.Event.wait``
  inside a supervised task), with a watched task whose heartbeat is stale past
  its bound — the exact condition the in-loop watchdog exists to trip on;
* the in-loop watchdog provably cannot fire (``os._exit`` is never reached
  while frozen) — the structural blindness, not a tuning problem;
* the monitor's file heartbeat (written by the supervisor's watchdog loop each
  time its sleep resumes) stops advancing the moment the loop freezes;
* the file-heartbeat-probe-based EXTERNAL watchdog — running outside the
  frozen loop, exactly as the separate ``alphamind-monitor-watchdog`` process
  does — observes the stale beat and fires the restart.

The injected sleep is scaled down (clock = sanctioned boundary) so the test
runs in well under a second of wall time.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest.mock as mock
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.process_supervision import (
    FileHeartbeatProbe,
    FileHeartbeatSink,
    run_watchdog,
)

_EXIT_PATH = "alphamind.execution.continuous_monitor.supervisor.os._exit"

# Real-time pacing for the simulated monitor: every injected sleep is capped at
# 10ms so the supervisor's watchdog loop beats the file heartbeat continuously
# while the loop turns, and the frozen phase is observed within ~100ms.
_MAX_REAL_SLEEP_SECONDS = 0.01
_FREEZER_CADENCE_SECONDS = 0.005  # stall bound = 0.05s at the 10x multiplier


class _RecordingController:
    """Records restart calls in place of the real NSSM shell-out."""

    def __init__(self) -> None:
        self.restart_calls = 0

    def restart(self) -> None:
        self.restart_calls += 1


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260609T120000Z-deadbeef",
        started_at=datetime.now(UTC),
        mode="paper",
    )


def _config() -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def test_frozen_loop_starves_in_loop_watchdog_but_external_watchdog_restarts(
    tmp_path: Path,
) -> None:
    heartbeat_path = tmp_path / "monitor.heartbeat"
    unfreeze = threading.Event()
    frozen = threading.Event()
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    async def _scaled_sleep(delay: float) -> None:
        await asyncio.sleep(min(delay, _MAX_REAL_SLEEP_SECONDS))

    supervisor = MonitorSupervisor(
        session=_session(),
        config=_config(),
        sleep=_scaled_sleep,
        heartbeat=FileHeartbeatSink(path=heartbeat_path),
    )

    async def _freezer(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        """A watched task that beats, then synchronously blocks the loop thread.

        After the block, its heartbeat is stale past its bound (0.05s) — the
        in-loop watchdog WOULD trip it if the loop were turning. The block is
        the production wedge: the whole loop thread is held, so neither this
        task nor the in-loop watchdog ever runs again until unfrozen.
        """
        del session, config
        loop_holder["loop"] = asyncio.get_running_loop()
        async for _ in supervisor.supervised_loop("freezer", _FREEZER_CADENCE_SECONDS):
            if heartbeat_path.exists():
                frozen.set()
                unfreeze.wait()  # SYNCHRONOUS block — the event loop is frozen.
                return

    supervisor.register_task(name="freezer", coro_fn=_freezer)

    exits: list[int] = []
    runner_error: list[BaseException] = []

    def _run_monitor() -> None:
        try:
            asyncio.run(supervisor.run())
        except BaseException as exc:  # surfaced after join — the thread must not die silently
            runner_error.append(exc)

    thread = threading.Thread(target=_run_monitor, name="frozen-monitor-loop")
    with mock.patch(_EXIT_PATH, side_effect=exits.append):
        thread.start()
        try:
            assert frozen.wait(timeout=10.0), "monitor loop never started beating"

            # --- frozen phase -------------------------------------------------
            beat_at_freeze = float(heartbeat_path.read_text(encoding="utf-8"))
            # Wait several multiples of the freezer's 0.05s stall bound: were
            # the loop turning, the in-loop watchdog would both advance the
            # heartbeat and trip the stale freezer task. Neither happens.
            time.sleep(0.3)
            assert float(heartbeat_path.read_text(encoding="utf-8")) == beat_at_freeze, (
                "heartbeat advanced while the loop was frozen"
            )
            assert exits == [], (
                "the in-loop watchdog reached os._exit on a frozen loop — "
                "structurally impossible; the simulation is broken"
            )

            # --- the external watchdog, outside the frozen loop --------------
            controller = _RecordingController()

            async def _one_tick() -> AsyncIterator[None]:
                yield

            asyncio.run(
                run_watchdog(
                    probe=FileHeartbeatProbe(path=heartbeat_path),
                    controller=controller,
                    stall_bound_seconds=0.05,
                    loop=lambda: _one_tick(),
                    now=lambda: beat_at_freeze + 999.0,  # well past any bound
                )
            )

            assert controller.restart_calls == 1
            assert exits == []  # still frozen; the in-loop watchdog never fired
        finally:
            unfreeze.set()
            loop = loop_holder.get("loop")
            if loop is not None:
                loop.call_soon_threadsafe(supervisor.request_stop)
            thread.join(timeout=10.0)

    assert not thread.is_alive(), "monitor loop thread failed to shut down"
    assert runner_error == []
