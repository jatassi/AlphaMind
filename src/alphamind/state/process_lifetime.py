"""Shared ``process_lifetimes`` row writer (story 01).

The only path that inserts a ``process_lifetimes`` row. Both the pipeline
scheduler (``alphamind.scheduler``) and the continuous monitor
(``alphamind.continuous_monitor``) call this helper exactly once at
process startup; every later invocation's ``invocations.process_lifetime_id``
FK depends on it.

Design references:

* ``docs/design/05-execution-layer/state-persistence.md`` § Process lifetimes
  — the 14-column row shape, append-only / immutable invariant.
* Parent issue ``ALP-431`` — coordination note placing the helper at this
  module path so both the scheduler and the monitor share it.

The implementation is fail-loud: any failure to gather a runtime
provenance field (e.g. ``git rev-parse HEAD`` returning non-zero) raises
before the row is inserted, leaving the database in its pre-call state.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import secrets
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.state.invocation_context.records import (
    ProcessLifetimeRecord,
    ProcessRole,
    process_lifetime_record_to_row,
)
from alphamind.state.invocation_context.snapshots import (
    write_pip_freeze_snapshot,
)


def _run_git(*args: str) -> str:
    """Run ``git`` with the supplied args and return stripped stdout.

    Raises ``subprocess.CalledProcessError`` on non-zero exit so a missing
    git binary, detached worktree, or repository corruption surfaces
    immediately rather than poisoning the row with stale provenance.
    """
    completed = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _capture_pip_freeze() -> str:
    """Return the installed-package set in ``pip freeze`` format.

    Uses :func:`importlib.metadata.distributions` directly rather than shelling
    out to ``python -m pip freeze`` so the helper works in any environment that
    can import its own metadata — notably ``uv``-managed venvs, which do not
    install pip by default. Output is sorted case-insensitively to match pip's
    own ordering convention.
    """
    lines = sorted(
        (f"{dist.name}=={dist.version}" for dist in importlib.metadata.distributions()),
        key=str.lower,
    )
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _format_started_at(started_at: datetime) -> str:
    """Format a tz-aware UTC datetime as ``YYYY-MM-DDTHH:MM:SSZ``.

    The terminal ``Z`` (rather than ``+00:00``) matches the rest of the
    state-persistence layer's ISO-8601 convention.
    """
    return started_at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_id(process_role: ProcessRole, started_at: datetime) -> str:
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    return f"plt-{process_role}-{stamp}-{secrets.token_hex(4)}"


def _package_version_or_absent(distribution_name: str) -> str:
    """Return the installed version of *distribution_name* or ``"not-installed"``.

    The state-persistence design pins both the Anthropic SDK and the Claude
    Agent SDK versions on every process_lifetime row. Either package can be
    absent in a particular environment (e.g. during distillation-only test
    runs); record the absence positively rather than failing the row.
    """
    try:
        return importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


async def record_process_lifetime(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    process_role: Literal["pipeline", "monitor"],
    archive_root: Path,
) -> str:
    """Insert one ``process_lifetimes`` row at process start, return its id.

    Gathers the 13 runtime-provenance fields (the 14th, ``process_lifetime_id``,
    is constructed locally), writes ``pip_freeze.txt`` under
    ``<archive_root>/process_lifetimes/<id>/``, and inserts the row in its own
    transaction. ``process_lifetimes`` is process-scoped, not invocation-scoped,
    so this commit is independent of any later ``invocations`` transaction.
    """
    started_at = datetime.now(UTC)

    git_sha = _run_git("rev-parse", "HEAD")
    git_branch = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    git_dirty = bool(_run_git("status", "--porcelain"))

    pip_freeze_text = _capture_pip_freeze()
    pip_freeze_hash = hashlib.sha256(pip_freeze_text.encode("utf-8")).hexdigest()

    process_lifetime_id = _build_id(process_role, started_at)
    pip_freeze_path = write_pip_freeze_snapshot(
        process_lifetime_id=process_lifetime_id,
        pip_freeze_text=pip_freeze_text,
        root=str(archive_root),
    )

    record = ProcessLifetimeRecord(
        process_lifetime_id=process_lifetime_id,
        process_role=process_role,
        process_start_at=_format_started_at(started_at),
        process_pid=os.getpid(),
        hostname=socket.gethostname(),
        git_sha=git_sha,
        git_branch=git_branch,
        git_dirty=git_dirty,
        python_version=sys.version,
        pip_freeze_hash=pip_freeze_hash,
        pip_freeze_snapshot_path=pip_freeze_path,
        anthropic_sdk_version=_package_version_or_absent("anthropic"),
        claude_agent_sdk_version=_package_version_or_absent("claude-agent-sdk"),
        os_release=platform.platform(),
    )

    row = process_lifetime_record_to_row(record)
    async with session_factory() as session:
        session.add(row)
        await session.commit()

    return process_lifetime_id
