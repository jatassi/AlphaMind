"""Command center backend package (ALP-128 / story 02 — ALP-666).

Re-exports the public surface stories 03+ will consume:

* :func:`build_app` — FastAPI composition root factory.
* :class:`CommandCenterSupervisor` — TaskGroup-rooted supervisor for the
  daemon's long-running tasks (Uvicorn + future SSE consumers + alert
  engine).
* :class:`ProcessSession` + :func:`new_session` — per-process handle the
  supervisor threads to its tasks.
* :data:`VERSION` — package version constant.

Internal subpackages (not re-exported; consumed by their own callers):

* :mod:`alphamind.command_center._kernel` — typed primitives (NewTypes,
  StrEnums, frozen-dataclass events, operator-invocation handle).
* :mod:`alphamind.command_center.persistence` — SQLAlchemy declarative
  tables for the three command-center-owned tables, row-↔-dataclass
  codecs, and the dual ``cc_writer`` / ``foreign_reader`` session
  factories.
* :mod:`alphamind.command_center.config` — Pydantic configuration
  models for ``config/command-center.yaml`` / ``config/security.yaml``
  / ``config/alerts.yaml``.
* :mod:`alphamind.command_center.logging_setup` — log rotation setup
  mirroring :func:`alphamind.scheduler.logging_setup.configure_pipeline_logging`.

See :mod:`alphamind.command_center.__main__` for the
``python -m alphamind.command_center`` entrypoint.
"""

from __future__ import annotations

from alphamind.command_center.app import build_app
from alphamind.command_center.session import ProcessSession, new_session
from alphamind.command_center.supervisor import CommandCenterSupervisor

__all__ = [
    "VERSION",
    "CommandCenterSupervisor",
    "ProcessSession",
    "build_app",
    "new_session",
]

VERSION = "0.1.0"
"""Command center version constant.

Tracked alongside the package surface so the public re-export contract is
stable; the value lives here rather than under ``app.py`` to keep the
re-export shape small (interface surface, not implementation).
"""
