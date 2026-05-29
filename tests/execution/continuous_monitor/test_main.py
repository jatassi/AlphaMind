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
from sqlalchemy import create_engine

import alphamind.state.tables  # noqa: F401 — register state-layer tables on Base.metadata
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
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")
    _ensure_db_schema(tmp_path)

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
    """Waves 2 + 3 + 4c — the daemon wires ``underlying_stream`` (02b),
    ``fill_stream_consumer`` (02c), ``greeks_refresh`` (03a),
    ``breach_loop`` (03b), and ``bracket_stops`` (04c) onto the supervisor.

    The patch on ``MonitorSupervisor.run`` captures ``self`` so we can read
    the registered task names without driving the asyncio loop.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")
    _ensure_db_schema(tmp_path)

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
    for expected in (
        "underlying_stream",
        "fill_stream_consumer",
        "greeks_refresh",
        "breach_loop",
        "bracket_stops",
        "entry_window",
        "borrow_accrual",
    ):
        assert expected in task_names, f"{expected} not registered; got {task_names!r}"


def test_main_shares_trigger_id_generator_across_breach_loop_and_bracket_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _silent_logger: None,
) -> None:
    """Regression — the cascade dispatcher's TriggerIdGenerator instance is
    also passed to ``register_options_bracket_watcher_task``.

    Per Fix 3 (ALP-123 PR-48 review): a bracket-stop fire and a cascade
    dispatch in the same session must consume from the *same* monotonic
    counter so the engine-originated ``client_order_id``
    (``MON.{session}.{trigger}.0``) cannot collide across producers.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv("ALPACA_PAPER_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "test-secret")
    _ensure_db_schema(tmp_path)

    captured: dict[str, object] = {}

    async def _no_op_run(self: object) -> None:
        captured["supervisor"] = self

    real_breach = "alphamind.execution.continuous_monitor.__main__._register_breach_loop"
    real_bracket = (
        "alphamind.execution.continuous_monitor.__main__.register_options_bracket_watcher_task"
    )

    def _capture_breach(*args: object, **kwargs: object) -> None:
        captured["breach_trigger_ids"] = kwargs.get("trigger_ids")

    def _capture_bracket(*args: object, **kwargs: object) -> None:
        captured["bracket_trigger_ids"] = kwargs.get("trigger_ids")

    with (
        mock.patch(
            "alphamind.execution.continuous_monitor.__main__.MonitorSupervisor.run",
            _no_op_run,
        ),
        mock.patch(real_breach, _capture_breach),
        mock.patch(real_bracket, _capture_bracket),
    ):
        monitor_main(["run", "--mode", "paper"])

    breach_trigger_ids = captured.get("breach_trigger_ids")
    bracket_trigger_ids = captured.get("bracket_trigger_ids")
    assert breach_trigger_ids is not None
    assert bracket_trigger_ids is not None
    # The same instance is threaded to both — not two independent generators.
    assert breach_trigger_ids is bracket_trigger_ids


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
