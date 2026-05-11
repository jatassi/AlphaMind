"""Per-invocation context bundle for ``run_invocation`` (ALP-450 item 6).

Bundles the seven process-stable inputs that previously threaded
individually through ``run_pipeline_scheduler_task`` → ``register_pipeline_jobs``
→ ``_make_scheduled_job`` → ``run_invocation`` (and symmetrically through
``run_emergency_receiver_task`` → ``_process_one_entry`` → ``run_invocation``).

Bundling them into one frozen dataclass means adding an eighth threaded
value (e.g. a new env-derived path) touches one location instead of three
layers in two paths.

The per-call dimensions — ``trigger_type``, ``trigger_source``,
``trigger_reason``, ``firing_run_type``, ``now`` — stay outside the
context because they vary on every call.
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
    """Frozen bundle of the seven process-stable inputs ``run_invocation`` reads."""

    session_factory: async_sessionmaker[AsyncSession]
    process_lifetime_id: str
    archive_root: Path
    config_dir: Path
    env_path: Path
    venue_config: VenueConfig
    execution_mode: ExecutionMode
