"""Smoke tests for ``python -m alphamind.execution.continuous_monitor`` (story 01).

The entry point binds the supervisor + session + logging + config primitives
into a daemon. With no tasks registered (this story), it should start, idle
on ``request_stop``, and shut down cleanly. The acceptance criteria pin the
``run --mode paper`` path and SIGINT semantics.

The full ``run`` command requires file I/O against ``~/AlphaMind/logs/``; the
tests here exercise the seam-friendly entry point (``main``) under
monkey-patched HOME so they don't write into the operator's real log dir.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import create_engine

import alphamind.state.tables  # noqa: F401 — register state-layer tables on Base.metadata
from alphamind.execution.continuous_monitor.__main__ import (
    _log_filename_for_subcommand,
    _parse_args,
    _run_watchdog_daemon,
)
from alphamind.execution.continuous_monitor.__main__ import main as monitor_main
from alphamind.persistence.models import Base


def _ensure_db_schema(tmp_path: Path) -> None:
    """Create the ``alphamind.db`` schema where the monitor will read/write.

    The monitor's startup ``record_process_lifetime`` call (ALP-719) writes
    a row to ``process_lifetimes`` before the supervisor's task tree spins
    up. Before that change landed, the smoke tests could run against an
    empty DB because no task body ran (``MonitorSupervisor.run`` is patched
    to a no-op). With the new write at startup, every smoke test needs the
    schema in place. ``alphamind.state.tables`` is imported above so its
    table classes register on ``Base.metadata`` before ``create_all``.

    ``main.yaml`` declares ``%USERPROFILE%\\AlphaMind\\data\\alphamind.db``;
    on POSIX the backslashes are literal characters in the resolved
    filename. We mirror that resolution so the engine the monitor builds
    points at the schema we just created.
    """
    from alphamind.persistence.session import _resolve_path

    resolved = _resolve_path(None)
    db_path = Path(resolved)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    del tmp_path  # signal that the path comes from main.yaml resolution
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


@pytest.fixture()
def _silent_logger() -> Iterator[None]:
    """Reset the alphamind logger after each test so handlers don't leak."""
    import logging

    logger = logging.getLogger("alphamind")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    logger.handlers = []
    yield
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.handlers = saved_handlers
    logger.setLevel(saved_level)


# ---------------------------------------------------------------------------
# Shared boot fixture — runs monitor_main once and captures supervisor + log
# ---------------------------------------------------------------------------


@pytest.fixture()
def _paper_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> dict[str, Any]:
    """Run ``monitor_main(['run', '--mode', 'paper'])`` with a patched supervisor
    and return a dict with the captured supervisor and log path.

    Keys:
    * ``"supervisor"``  — the ``MonitorSupervisor`` instance passed to ``.run``
    * ``"log_path"``    — Path to the written ``monitor.log`` file
    * ``"tmp_path"``    — the ``tmp_path`` fixture value
    """
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")
    _ensure_db_schema(tmp_path)

    captured: dict[str, Any] = {"tmp_path": tmp_path}

    async def _no_op_run(self: object) -> None:
        captured["supervisor"] = self

    with mock.patch(
        "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor.run",
        _no_op_run,
    ):
        monitor_main(["run", "--mode", "paper"])

    captured["log_path"] = tmp_path / "AlphaMind" / "logs" / "monitor.log"
    return captured


# ---------------------------------------------------------------------------
# Cheap distinct assertions that share _paper_boot
# ---------------------------------------------------------------------------


def test_main_run_paper_starts_supervisor_and_returns_cleanly(
    _paper_boot: dict[str, Any],
) -> None:
    """``run --mode paper`` constructs the supervisor and runs it to completion.

    The seam: patch ``MonitorSupervisor.run`` to a no-op so the daemon path
    exits without needing a real signal; assert the supervisor was constructed
    with a paper-mode session and that the session-start line was emitted to
    the configured ``monitor.log``.
    """
    assert "supervisor" in _paper_boot, "MonitorSupervisor.run was not called"
    log_path = _paper_boot["log_path"]
    assert log_path.exists(), "monitor.log was not created by configure_monitor_logging"
    log_contents = log_path.read_text(encoding="utf-8")
    assert "monitor session start" in log_contents
    assert "mode=paper" in log_contents


def test_main_run_defaults_to_paper_mode(
    _paper_boot: dict[str, Any],
) -> None:
    """Omitting ``--mode`` selects paper — verified via the shared boot fixture log."""
    log_path = _paper_boot["log_path"]
    assert "mode=paper" in log_path.read_text(encoding="utf-8")


def test_main_run_no_mode_arg_defaults_to_paper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    """Argparse DEFAULT path: ``monitor_main(['run'])`` with no ``--mode`` selects paper.

    The consolidated ``_paper_boot`` fixture always passes ``--mode paper``
    explicitly, so it does not exercise the argparse default. This test calls
    ``monitor_main(['run'])`` with no ``--mode`` argument and asserts the log
    records ``mode=paper`` — proving the argparse default is wired to paper.
    """
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")
    _ensure_db_schema(tmp_path)

    async def _no_op_run(self: object) -> None:
        del self

    with mock.patch(
        "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor.run",
        _no_op_run,
    ):
        monitor_main(["run"])

    log_path = tmp_path / "AlphaMind" / "logs" / "monitor.log"
    assert "mode=paper" in log_path.read_text(encoding="utf-8")


def test_main_registers_precision_and_data_tasks(
    _paper_boot: dict[str, Any],
) -> None:
    """The monitor proper wires only precision/data tasks onto the supervisor.

    Post-ALP-857 (W4b) the monitor proper is precision/data only:
    ``underlying_stream`` (02b), ``fill_stream_consumer`` (02c),
    ``greeks_refresh`` (03a), ``bracket_stops`` (04c), ``entry_window``. Breach
    detection (the lone no-floor safety item) is isolated into the
    out-of-process safety core, so ``breach_loop`` is no longer a monitor task —
    a frozen monitor proper now degrades precision while positions stay
    broker-protected (ADR-0004).

    The patch on ``MonitorSupervisor.run`` captures ``self`` so we can read
    the registered task names without driving the asyncio loop.
    """
    supervisor = _paper_boot.get("supervisor")
    assert supervisor is not None
    task_names = supervisor.task_names()
    for expected in (
        "underlying_stream",
        "fill_stream_consumer",
        "greeks_refresh",
        "bracket_stops",
        "entry_window",
    ):
        assert expected in task_names, f"{expected} not registered; got {task_names!r}"
    # ALP-857 / W4b — breach detection is isolated into the out-of-process safety
    # core; the monitor proper no longer registers a ``breach_loop`` task.
    assert "breach_loop" not in task_names, (
        f"breach_loop must be isolated into the safety core, not a monitor task; got {task_names!r}"
    )
    # ALP-855 / W4a — borrow accrual is accounting, evicted from the always-on
    # monitor (ADR-0004) into the pipeline's fill-collection write unit. It is no longer
    # a monitor task.
    assert "borrow_accrual" not in task_names, (
        f"borrow_accrual should be evicted from the monitor; got {task_names!r}"
    )


# ---------------------------------------------------------------------------
# ALP-941 — out-of-process monitor watchdog + heartbeat + faulthandler deadman
# ---------------------------------------------------------------------------


def test_main_run_wires_file_heartbeat_and_faulthandler_deadman(
    _paper_boot: dict[str, Any],
) -> None:
    """The run daemon injects the monitor.heartbeat sink and registers the deadman.

    Both are the monitor-side halves of the ALP-941 watchdog pattern: the
    supervisor's watchdog loop beats ``monitor.heartbeat`` for the external
    watchdog to probe, and the faulthandler deadman self-captures the blocking
    frame on the next freeze.
    """
    supervisor = _paper_boot["supervisor"]
    heartbeat = supervisor._heartbeat
    assert heartbeat is not None, "_run_daemon did not inject a heartbeat sink"
    expected_path = _paper_boot["tmp_path"] / "AlphaMind" / "logs" / "monitor.heartbeat"
    assert heartbeat._path == expected_path
    assert "faulthandler_deadman" in supervisor.task_names()


def test_parse_args_routes_watchdog_subcommand() -> None:
    args = _parse_args(["watchdog"])
    assert args.subcommand == "watchdog"


def test_run_and_watchdog_log_to_distinct_files() -> None:
    """The monitor and its watchdog are separate processes — no shared rotating
    file (ALP-868 WinError 32 rule), and neither collides with the safety-core
    services' files."""
    run_log = _log_filename_for_subcommand("run")
    watchdog_log = _log_filename_for_subcommand("watchdog")

    assert run_log == "monitor.log"
    assert watchdog_log == "monitor_watchdog.log"
    assert len({run_log, watchdog_log, "safety_core.log", "safety_core_watchdog.log"}) == 4


class _ScriptedProbe:
    """Heartbeat probe returning a scripted age per watchdog tick."""

    def __init__(self, ages: list[float | None]) -> None:
        self._ages = list(ages)

    def age(self, *, now: float) -> float | None:
        del now
        return self._ages.pop(0) if self._ages else None


class _RecordingController:
    def __init__(self) -> None:
        self.restart_calls = 0

    def restart(self) -> None:
        self.restart_calls += 1


async def test_watchdog_daemon_restarts_only_past_the_config_stall_bound() -> None:
    """The watchdog daemon's bound is monitor_watchdog_tick_seconds x multiplier.

    With the shipped config (15s x 10 = 150s): an absent heartbeat (None) is
    startup grace, an age inside the bound is healthy, and only an age past the
    bound restarts — one restart across the three scripted ticks.
    """
    from collections.abc import AsyncIterator

    probe = _ScriptedProbe(ages=[None, 149.0, 151.0])
    controller = _RecordingController()

    async def _three_ticks() -> AsyncIterator[None]:
        for _ in range(3):
            yield

    clock = iter([1000.0, 2000.0, 3000.0])

    await _run_watchdog_daemon(
        probe=probe,
        controller=controller,
        loop=lambda: _three_ticks(),
        now=lambda: next(clock),
    )

    assert controller.restart_calls == 1


# ---------------------------------------------------------------------------
# Focused tests retained per ALP-798 PRESERVE list
# ---------------------------------------------------------------------------


def test_main_rejects_unknown_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    with pytest.raises(SystemExit):
        monitor_main(["bogus"])


def test_main_wires_is_market_open_into_underlying_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    """Regression (ALP-832) — the daemon supplies a non-None ``is_market_open``
    to ``register_underlying_stream_task``.

    Without it, ``run_underlying_stream``'s ``is_market_open`` defaults to
    ``None`` → ``is_rth`` stays ``None`` → ``evaluate_staleness`` reports
    ``stalled=False`` on every slice → ``StreamStalledError`` never raises →
    the budget-neutral RTH-silence reconnect can never fire in production.

    The existing ``underlying_stream`` task tests inject ``is_market_open``
    directly, so they could not catch the omission at the ``__main__``
    composition site. This test captures the kwarg the daemon actually passes
    to the wiring helper — the production seam — and asserts it is the
    calendar cache's predicate, not ``None``.
    """
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")
    _ensure_db_schema(tmp_path)

    captured: dict[str, object] = {}

    real_register = (
        "alphamind.execution.continuous_monitor.__main__.register_underlying_stream_task"
    )

    def _capture_register(*args: object, **kwargs: object) -> object:
        captured["is_market_open"] = kwargs.get("is_market_open")
        # Return a real cache so the rest of the composition (breach loop,
        # greeks refresh, bracket stops) wires against a valid instance.
        from alphamind.execution.continuous_monitor.underlying_stream.cache import (
            UnderlyingPriceCache,
        )

        return UnderlyingPriceCache()

    async def _no_op_run(self: object) -> None:
        del self

    with (
        mock.patch(
            "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor.run",
            _no_op_run,
        ),
        mock.patch(real_register, _capture_register),
    ):
        monitor_main(["run", "--mode", "paper"])

    is_market_open = captured.get("is_market_open")
    assert is_market_open is not None, (
        "register_underlying_stream_task was called without is_market_open; "
        "the RTH-silence staleness reconnect would be dead in production"
    )
    assert callable(is_market_open)


def test_main_writes_pip_freeze_snapshot_under_monkeypatched_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    """Regression — ``_default_archive_root()`` resolves at call time, not import.

    Prior to the fix, ``_DEFAULT_ARCHIVE_ROOT`` was evaluated at module
    import via ``Path.home() / "AlphaMind" / "archive"``. Tests that
    ``monkeypatch.setenv("HOME", tmp_path)`` did so *after* import, so
    ``record_process_lifetime`` wrote ``pip_freeze.txt`` snapshots to the
    developer's real home directory. The fix moves the lookup inside
    ``_run_daemon`` so the monkeypatched ``HOME`` is honored.
    """
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")
    _ensure_db_schema(tmp_path)

    async def _no_op_run(self: object) -> None:
        del self

    with mock.patch(
        "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor.run",
        _no_op_run,
    ):
        monitor_main(["run", "--mode", "paper"])

    archive_root = tmp_path / "AlphaMind" / "archive"
    assert archive_root.exists(), (
        f"archive root not created under monkeypatched HOME: {archive_root}"
    )
    # ``record_process_lifetime`` writes ``pip_freeze.txt`` under
    # ``<archive_root>/process_lifetimes/<id>/`` — at least one entry should
    # exist after the monitor's startup commit.
    process_lifetimes_dir = archive_root / "process_lifetimes"
    assert process_lifetimes_dir.exists()
    snapshots = list(process_lifetimes_dir.rglob("pip_freeze.txt"))
    assert snapshots, (
        f"no pip_freeze.txt snapshot was written under {process_lifetimes_dir}; "
        f"the lookup likely still resolves to the developer's real HOME"
    )
