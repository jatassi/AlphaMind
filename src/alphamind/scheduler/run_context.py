"""Per-invocation context bundle for ``run_invocation``.

Bundles the process-stable inputs threaded through the supervisor task
layers down to ``run_invocation``. The per-call dimensions
(``trigger_type``, ``trigger_source``, ``trigger_reason``,
``firing_run_type``, ``now``) stay outside the context because they
vary on every call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig

if TYPE_CHECKING:
    # ``DebugE2ESettings`` lives in ``scheduler/debug_e2e/settings.py``
    # (story 03 / ALP-496). The import is ``TYPE_CHECKING``-only so the
    # runtime import graph never reaches the debug-only package — story
    # 04 / ALP-500 lands an import-linter contract that enforces this
    # at module-load time. Anything in this module that needs the type
    # carries the forward-reference string annotation
    # ``DebugE2ESettings | None``.
    from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings

__all__ = ["RunInvocationContext"]


@dataclass(frozen=True, slots=True)
class RunInvocationContext:
    """Frozen bundle of the process-stable inputs ``run_invocation`` reads.

    Two session factories travel together: the async one drives fill-collection /
    command-execution writes through ``aiosqlite``; the sync one backs the analysis
    pipeline's between-phase read, where the distillation orchestrator's
    ``asyncio.to_thread`` callees consume a sync SQLAlchemy ``Session`` API.

    ``debug_e2e`` is the single indicator the orchestrator inspects to
    detect debug-e2e mode (P3 — no parallel boolean flag). Its presence
    swaps the broker adapters and progress emitter; production callers
    leave it at the default ``None``.
    """

    session_factory: async_sessionmaker[AsyncSession]
    sync_session_factory: sessionmaker[Session]
    process_lifetime_id: str
    archive_root: Path
    config_dir: Path
    env_path: Path
    venue_config: VenueConfig
    execution_mode: ExecutionMode
    debug_e2e: DebugE2ESettings | None = None
