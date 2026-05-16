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

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig

__all__ = ["RunInvocationContext"]


@dataclass(frozen=True, slots=True)
class RunInvocationContext:
    """Frozen bundle of the process-stable inputs ``run_invocation`` reads.

    Two session factories travel together: the async one drives Phase 1 /
    Phase 2 writes through ``aiosqlite``; the sync one backs the analysis
    pipeline's between-phase read, where the distillation orchestrator's
    ``asyncio.to_thread`` callees consume a sync SQLAlchemy ``Session`` API.
    """

    session_factory: async_sessionmaker[AsyncSession]
    sync_session_factory: sessionmaker[Session]
    process_lifetime_id: str
    archive_root: Path
    config_dir: Path
    env_path: Path
    venue_config: VenueConfig
    execution_mode: ExecutionMode
