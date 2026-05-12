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
from unittest import mock

import pytest

from alphamind.execution.continuous_monitor.__main__ import main as monitor_main


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


def test_main_run_paper_starts_supervisor_and_returns_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    """``run --mode paper`` constructs the supervisor and runs it to completion.

    The seam: patch ``MonitorSupervisor.run`` to a no-op so the daemon path
    exits without needing a real signal; assert the supervisor was constructed
    with a paper-mode session and that the session-start line was emitted to
    the configured ``monitor.log``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)

    constructed: dict[str, object] = {}

    real_supervisor_cls = "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor"

    async def _no_op_run(self: object) -> None:
        constructed["supervisor"] = self

    with mock.patch(f"{real_supervisor_cls}.run", _no_op_run):
        monitor_main(["run", "--mode", "paper"])

    assert "supervisor" in constructed, "MonitorSupervisor.run was not called"

    log_path = tmp_path / "AlphaMind" / "logs" / "monitor.log"
    assert log_path.exists(), "monitor.log was not created by configure_monitor_logging"
    log_contents = log_path.read_text(encoding="utf-8")
    assert "monitor session start" in log_contents
    assert "mode=paper" in log_contents


def test_main_run_defaults_to_paper_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    """Omitting ``--mode`` selects paper."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)

    async def _no_op_run(self: object) -> None:
        del self

    with mock.patch(
        "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor.run",
        _no_op_run,
    ):
        monitor_main(["run"])

    log_path = tmp_path / "AlphaMind" / "logs" / "monitor.log"
    assert "mode=paper" in log_path.read_text(encoding="utf-8")


def test_main_rejects_unknown_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    with pytest.raises(SystemExit):
        monitor_main(["bogus"])


def test_main_registers_wave_2_and_3_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    """Waves 2 + 3 — the daemon wires ``underlying_stream`` (02b),
    ``fill_stream_consumer`` (02c), ``greeks_refresh`` (03a), and
    ``breach_loop`` (03b) onto the supervisor.

    The patch on ``MonitorSupervisor.run`` captures ``self`` so we can read
    the registered task names without driving the asyncio loop.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")

    captured: dict[str, object] = {}

    async def _no_op_run(self: object) -> None:
        captured["supervisor"] = self

    with mock.patch(
        "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor.run",
        _no_op_run,
    ):
        monitor_main(["run", "--mode", "paper"])

    supervisor = captured.get("supervisor")
    assert supervisor is not None
    task_names = supervisor.task_names()  # type: ignore[attr-defined]
    for expected in ("underlying_stream", "fill_stream_consumer", "greeks_refresh", "breach_loop"):
        assert expected in task_names, f"{expected} not registered; got {task_names!r}"
