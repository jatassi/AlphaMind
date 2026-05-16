"""Tests for ``RunInvocationContext``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from alphamind.scheduler.run_context import RunInvocationContext


def test_dataclass_is_frozen() -> None:
    """Mutating a field raises ``FrozenInstanceError``.

    The frozen contract is the only behaviour the dataclass adds beyond
    its declared shape; the field set itself is enforced at every
    callsite by the type-checker.
    """
    ctx = RunInvocationContext(
        session_factory="session-factory-sentinel",  # type: ignore[arg-type]
        sync_session_factory="sync-session-factory-sentinel",  # type: ignore[arg-type]
        process_lifetime_id="proc-1",
        archive_root=Path("/tmp/archive"),
        config_dir=Path("/tmp/config"),
        env_path=Path("/tmp/.env"),
        venue_config="venue-config-sentinel",  # type: ignore[arg-type]
        execution_mode="paper-sentinel",  # type: ignore[arg-type]
    )
    with pytest.raises(FrozenInstanceError):
        ctx.process_lifetime_id = "proc-2"  # type: ignore[misc]
