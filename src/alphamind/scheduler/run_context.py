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

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig

__all__ = ["RunInvocationContext"]


@dataclass(frozen=True, slots=True)
class RunInvocationContext:
    """Frozen bundle of the process-stable inputs ``run_invocation`` reads."""

    session_factory: async_sessionmaker[AsyncSession]
    process_lifetime_id: str
    archive_root: Path
    config_dir: Path
    env_path: Path
    venue_config: VenueConfig
    execution_mode: ExecutionMode
