"""Tests for ``command_center.logging_setup`` (story 02 / ALP-666).

Mirrors :mod:`tests.scheduler.test_logging_setup` in shape — covers the
log-file path resolution + the idempotent handler-attachment property.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from alphamind.command_center.logging_setup import configure_command_center_logging


@pytest.fixture
def alphamind_root_handlers() -> Iterator[list[logging.Handler]]:
    """Snapshot + restore the ``alphamind`` root logger's handler list.

    The logging setup is idempotent but installs a handler on a process-
    global logger; isolate tests so a fixture failure doesn't leak a
    handler into subsequent tests.
    """
    root = logging.getLogger("alphamind")
    saved = list(root.handlers)
    root.handlers.clear()
    try:
        yield root.handlers
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)


class TestConfigureCommandCenterLogging:
    def test_attaches_rotating_handler_under_userprofile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alphamind_root_handlers: list[logging.Handler],
    ) -> None:
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        configure_command_center_logging()
        log_file = tmp_path / "AlphaMind" / "logs" / "command_center.log"
        assert log_file.parent.exists()
        # The handler is attached; its baseFilename targets command_center.log.
        rotating = [
            h
            for h in alphamind_root_handlers
            if isinstance(h, TimedRotatingFileHandler)
            and Path(h.baseFilename).name == "command_center.log"
        ]
        assert len(rotating) == 1

    def test_is_idempotent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alphamind_root_handlers: list[logging.Handler],
    ) -> None:
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        configure_command_center_logging()
        configure_command_center_logging()
        rotating = [
            h
            for h in alphamind_root_handlers
            if isinstance(h, TimedRotatingFileHandler)
            and Path(h.baseFilename).name == "command_center.log"
        ]
        assert len(rotating) == 1

    def test_does_not_collide_with_pipeline_log_handler(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alphamind_root_handlers: list[logging.Handler],
    ) -> None:
        """The cc logging setup must not skip its install when a sibling
        pipeline.log handler is already attached — they are independent."""
        from alphamind.scheduler.logging_setup import configure_pipeline_logging

        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        configure_pipeline_logging()
        configure_command_center_logging()
        rotating_names = {
            Path(h.baseFilename).name
            for h in alphamind_root_handlers
            if isinstance(h, TimedRotatingFileHandler)
        }
        assert "pipeline.log" in rotating_names
        assert "command_center.log" in rotating_names

    def test_respects_log_level_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alphamind_root_handlers: list[logging.Handler],
    ) -> None:
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        configure_command_center_logging()
        assert logging.getLogger("alphamind").level == logging.WARNING

    def test_falls_back_to_home_without_userprofile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        alphamind_root_handlers: list[logging.Handler],
    ) -> None:
        """POSIX path — when USERPROFILE is unset, falls back to home()."""
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.setattr("alphamind.command_center.logging_setup.Path.home", lambda: tmp_path)
        configure_command_center_logging()
        log_file = tmp_path / "AlphaMind" / "logs" / "command_center.log"
        assert log_file.parent.is_dir()
        # The fixture is needed to isolate the handler list per test, even
        # though this assertion only inspects the filesystem.
        assert alphamind_root_handlers is not None
