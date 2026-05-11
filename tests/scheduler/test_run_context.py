"""Tests for ``RunInvocationContext`` (ALP-450 item 6)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from alphamind.scheduler.run_context import RunInvocationContext


def _make_context() -> RunInvocationContext:
    """Build a context with every field set to a sentinel.

    The field types are not enforced by the dataclass (it's a typed bundle,
    not a validator); using plain values keeps the test independent of the
    upstream session / venue construction.
    """
    return RunInvocationContext(
        session_factory="session-factory-sentinel",  # type: ignore[arg-type]
        process_lifetime_id="proc-1",
        archive_root=Path("/tmp/archive"),
        config_dir=Path("/tmp/config"),
        env_path=Path("/tmp/.env"),
        venue_config="venue-config-sentinel",  # type: ignore[arg-type]
        execution_mode="paper-sentinel",  # type: ignore[arg-type]
    )


def test_dataclass_has_seven_bundled_fields() -> None:
    """The seven fields ALP-450 item 6 names appear on the dataclass."""
    field_names = {f.name for f in fields(RunInvocationContext)}
    assert field_names == {
        "session_factory",
        "process_lifetime_id",
        "archive_root",
        "config_dir",
        "env_path",
        "venue_config",
        "execution_mode",
    }


def test_dataclass_is_frozen() -> None:
    """The dataclass is frozen — mutating a field raises ``FrozenInstanceError``."""
    ctx = _make_context()
    with pytest.raises(FrozenInstanceError):
        ctx.process_lifetime_id = "proc-2"  # type: ignore[misc]
