"""Tests for ``configure_pipeline_logging`` (story 01)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from alphamind.scheduler.logging_setup import configure_pipeline_logging


@pytest.fixture()
def isolated_logger() -> Iterator[logging.Logger]:
    """Snapshot/restore the ``alphamind`` logger's handlers + level."""
    logger = logging.getLogger("alphamind")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    logger.handlers = []
    try:
        yield logger
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)


class TestConfigurePipelineLogging:
    def test_installs_timed_rotating_handler_under_alphamind_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_logger: logging.Logger,
    ) -> None:
        # Redirect $HOME so we don't touch the operator's real logs dir.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        configure_pipeline_logging()

        rotating = [h for h in isolated_logger.handlers if isinstance(h, TimedRotatingFileHandler)]
        assert len(rotating) == 1
        handler = rotating[0]
        assert Path(handler.baseFilename) == tmp_path / "AlphaMind" / "logs" / "pipeline.log"
        assert handler.when == "MIDNIGHT"
        assert handler.interval == 24 * 60 * 60
        assert handler.backupCount == 30

    def test_honours_userprofile_on_windows_style_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_logger: logging.Logger,
    ) -> None:
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path / "other"))

        configure_pipeline_logging()

        rotating = [h for h in isolated_logger.handlers if isinstance(h, TimedRotatingFileHandler)]
        assert len(rotating) == 1
        assert Path(rotating[0].baseFilename) == tmp_path / "AlphaMind" / "logs" / "pipeline.log"

    def test_is_idempotent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_logger: logging.Logger,
    ) -> None:
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        configure_pipeline_logging()
        configure_pipeline_logging()

        rotating = [h for h in isolated_logger.handlers if isinstance(h, TimedRotatingFileHandler)]
        assert len(rotating) == 1, "duplicate handlers attached on repeat calls"

    def test_creates_log_directory_if_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_logger: logging.Logger,
    ) -> None:
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert not (tmp_path / "AlphaMind" / "logs").exists()

        configure_pipeline_logging()

        assert (tmp_path / "AlphaMind" / "logs").is_dir()

    def test_logger_name_is_alphamind_so_collector_and_monitor_share_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_logger: logging.Logger,
    ) -> None:
        # The named logger that receives the handler must be "alphamind" so
        # descendant loggers (collector, monitor, scheduler modules) propagate
        # into the same file handler.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        configure_pipeline_logging()

        assert any(
            isinstance(h, TimedRotatingFileHandler) for h in logging.getLogger("alphamind").handlers
        )
