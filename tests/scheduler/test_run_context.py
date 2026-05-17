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


def test_debug_e2e_defaults_to_none() -> None:
    """``debug_e2e`` is an optional field defaulting to ``None``.

    Story ALP-495 introduces this field; every existing constructor
    must continue to work without specifying it.
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
    assert ctx.debug_e2e is None


def test_debug_e2e_accepts_sentinel_value() -> None:
    """Explicit ``debug_e2e=<settings>`` round-trips via the constructor.

    The field is forward-referenced via ``TYPE_CHECKING`` because the
    concrete ``DebugE2ESettings`` lives in story 03's ``settings.py``
    and would otherwise create the cycle the import-linter contract
    (story 04) forbids. Any opaque sentinel must therefore be storable.
    """
    sentinel = object()
    ctx = RunInvocationContext(
        session_factory="session-factory-sentinel",  # type: ignore[arg-type]
        sync_session_factory="sync-session-factory-sentinel",  # type: ignore[arg-type]
        process_lifetime_id="proc-1",
        archive_root=Path("/tmp/archive"),
        config_dir=Path("/tmp/config"),
        env_path=Path("/tmp/.env"),
        venue_config="venue-config-sentinel",  # type: ignore[arg-type]
        execution_mode="paper-sentinel",  # type: ignore[arg-type]
        debug_e2e=sentinel,
    )
    assert ctx.debug_e2e is sentinel


def test_run_context_does_not_eagerly_import_debug_e2e() -> None:
    """``run_context`` must not trigger import of ``debug_e2e`` at module-load.

    Story 04 lands an import-linter contract forbidding the import;
    the forward reference must be ``TYPE_CHECKING``-only.

    Run in a fresh subprocess so the assertion is unaffected by other tests in
    the same xdist worker that may have imported ``debug_e2e`` already.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "import alphamind.scheduler.run_context  # noqa: F401\n"
                "assert 'alphamind.scheduler.debug_e2e' not in sys.modules, (\n"
                "    'debug_e2e imported eagerly via run_context'\n"
                ")\n"
                "assert 'alphamind.scheduler.debug_e2e.settings' not in sys.modules, (\n"
                "    'debug_e2e.settings imported eagerly via run_context'\n"
                ")\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
