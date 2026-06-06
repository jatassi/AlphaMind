"""Rotating-file logging setup for the continuous monitor and safety core (story 01).

Mirrors ``alphamind.collector.scheduler._configure_logging`` and
``alphamind.scheduler.logging_setup.configure_pipeline_logging`` — same
``TimedRotatingFileHandler`` shape, same 30-day retention, same shared root
logger name ``alphamind`` so collector / scheduler / monitor share a
configurable level. Writes to ``%USERPROFILE%\\AlphaMind\\logs\\<filename>`` on
Windows and ``~/AlphaMind/logs/<filename>`` on POSIX, where ``filename`` defaults
to ``monitor.log`` (the safety core passes its own — ALP-868).
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def _log_directory() -> Path:
    """Resolve the AlphaMind log directory.

    Honour ``USERPROFILE`` when set (Windows convention used in production)
    and fall back to ``Path.home()`` on POSIX. This matches the precedent in
    ``alphamind.collector.scheduler._configure_logging`` and
    ``alphamind.scheduler.logging_setup._log_directory``.
    """
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / "AlphaMind" / "logs"


def configure_monitor_logging(filename: str = "monitor.log") -> None:
    """Install a TimedRotatingFileHandler writing to ``filename`` in the log dir.

    ``filename`` defaults to ``monitor.log`` (the continuous monitor). A separate
    process sharing this helper — the isolated safety core (ALP-868) — passes its
    own filename so no two processes rotate the same file: on Windows
    ``TimedRotatingFileHandler`` cannot rename a file another process holds open
    (``WinError 32``).

    Idempotent: a repeat call returns without attaching a duplicate handler.
    """
    log_dir = _log_directory()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / filename

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("alphamind")
    root.setLevel(level)

    if any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
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
