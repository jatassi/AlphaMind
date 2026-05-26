"""``command_center.log`` rotation setup (story 02 / ALP-666).

Mirrors :func:`alphamind.scheduler.logging_setup.configure_pipeline_logging`
shape: ``TimedRotatingFileHandler`` writing daily-rotated UTF-8 files
under the standard ``%USERPROFILE%\\AlphaMind\\logs\\`` directory, attached
to the shared ``alphamind`` root logger so the collector / scheduler /
monitor / command-center processes all share a configurable level.

Idempotent: a repeat call returns without attaching a duplicate handler.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def _log_directory() -> Path:
    """Resolve the AlphaMind log directory.

    Honour ``USERPROFILE`` when set (Windows convention used in production)
    and fall back to ``Path.home()`` on POSIX. This matches the precedent
    in :func:`alphamind.scheduler.logging_setup.configure_pipeline_logging`.
    """
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / "AlphaMind" / "logs"


def configure_command_center_logging() -> None:
    """Install a TimedRotatingFileHandler writing to ``command_center.log``.

    Idempotent: a repeat call returns without attaching a duplicate
    handler. Sets the shared ``alphamind`` root logger's level from
    the ``LOG_LEVEL`` env var (default ``INFO``).
    """
    log_dir = _log_directory()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "command_center.log"

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("alphamind")
    root.setLevel(level)

    if any(
        isinstance(h, TimedRotatingFileHandler) and getattr(h, "baseFilename", "") == str(log_file)
        for h in root.handlers
    ):
        return

    handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
