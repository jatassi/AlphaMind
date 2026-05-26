"""Command center backend package (ALP-128 / story 02 — ALP-666).

Top-level surface re-exports are added once the underlying modules ship in
this story's later commits. Subpackage layout:

* :mod:`alphamind.command_center._kernel` — typed primitives (NewTypes,
  StrEnums, frozen-dataclass events, operator-invocation handle).
* :mod:`alphamind.command_center.persistence` — SQLAlchemy declarative
  tables for the three command-center-owned tables, row-↔-dataclass
  codecs, and the dual ``cc_writer`` / ``foreign_reader`` session factories.
* :mod:`alphamind.command_center.app` — FastAPI app composition root.
* :mod:`alphamind.command_center.config` — Pydantic configuration models
  for ``config/command-center.yaml`` / ``config/security.yaml`` /
  ``config/alerts.yaml``.
* :mod:`alphamind.command_center.supervisor` — ``asyncio.TaskGroup``-rooted
  supervisor mirroring :mod:`alphamind.scheduler.supervisor`.
* :mod:`alphamind.command_center.session` — process-lifetime context manager
  (signal handling, NSSM-friendly logging redirect).
* :mod:`alphamind.command_center.logging_setup` — log rotation setup
  mirroring :func:`alphamind.scheduler.logging_setup.configure_pipeline_logging`.

See :mod:`alphamind.command_center.__main__` for the
``python -m alphamind.command_center`` entrypoint.
"""

from __future__ import annotations

__all__ = ["VERSION"]

VERSION = "0.1.0"
"""Command center version constant.

Tracked alongside the package surface so the public re-export contract is
stable; the value lives here rather than under ``app.py`` to keep the
re-export shape small (interface surface, not implementation).
"""
