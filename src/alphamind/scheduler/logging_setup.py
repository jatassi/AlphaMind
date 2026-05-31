"""``pipeline.log`` rotation setup for the scheduler.

Mirrors ``alphamind.collector.scheduler._configure_logging`` shape — same
``TimedRotatingFileHandler``, same 30-day retention, same shared root
logger name ``alphamind`` so collector / scheduler / monitor share a
configurable level. Writes to ``%USERPROFILE%\\AlphaMind\\logs\\pipeline.log``
on Windows and ``~/AlphaMind/logs/pipeline.log`` on POSIX.
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
    in ``alphamind.collector.scheduler._configure_logging``.
    """
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / "AlphaMind" / "logs"


def configure_pipeline_logging() -> None:
    """Install a TimedRotatingFileHandler writing to ``pipeline.log``.

    Idempotent: a repeat call returns without attaching a duplicate handler.
    """
    log_dir = _log_directory()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pipeline.log"

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
