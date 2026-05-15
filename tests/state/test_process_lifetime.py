"""Tests for the shared ``record_process_lifetime`` helper (story 01).

The helper is the only path that inserts a ``process_lifetimes`` row. Both
the pipeline scheduler and the continuous monitor call it at process
startup; every later invocation's FK depends on it. The tests pin the
row's 13 captured fields, the on-disk ``pip_freeze.txt`` artefact, and
the fail-loud git-failure behavior so a future change cannot silently
ship a row with stale provenance.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.state.process_lifetime import (
    record_process_lifetime,
)
from alphamind.state.tables.process_lifetimes import (
    ProcessLifetimeRow,
)


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    # Ensure the table mappers are registered before create_all.
    import alphamind.state.tables  # noqa: F401

    eng = make_async_engine(":memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture()
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_async_session_factory(engine)


def _git_rev_parse_stub(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Stand-in for ``subprocess.run`` that mimics a clean repo state."""
    # Both ``git ...`` and ``<python> -m pip freeze`` call sites route through
    # the same patched ``subprocess.run`` — match on argv tail so the test
    # doesn't depend on the python interpreter path.
    if "pip" in args and "freeze" in args:
        return subprocess.CompletedProcess(args, returncode=0, stdout="pkg==1.0\n", stderr="")
    cmd = args[1:] if args and args[0] == "git" else args
    if cmd[:2] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="a" * 40 + "\n", stderr="")
    if cmd[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="main\n", stderr="")
    if cmd[:2] == ["status", "--porcelain"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected subprocess invocation in test: {args!r}")


def _patch_subprocess(stub: Any = _git_rev_parse_stub) -> Any:
    """Patch subprocess.run inside the helper module to return a stub."""
    return patch(
        "alphamind.state.process_lifetime.subprocess.run",
        side_effect=lambda args, **kwargs: stub(args, **kwargs),
    )


async def _read_single_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> ProcessLifetimeRow:
    async with session_factory() as sess:
        result = await sess.execute(select(ProcessLifetimeRow))
        rows = list(result.scalars())
    assert len(rows) == 1, f"expected exactly one process_lifetimes row, got {len(rows)}"
    return rows[0]


class TestRecordProcessLifetimeHappyPath:
    async def test_inserts_one_row_with_gathered_fields(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        with _patch_subprocess():
            plt_id = await record_process_lifetime(
                session_factory=session_factory,
                process_role="pipeline",
                archive_root=tmp_path,
            )

        row = await _read_single_row(session_factory)
        assert row.process_lifetime_id == plt_id
        assert plt_id.startswith("plt-pipeline-")
        assert row.process_role == "pipeline"
        # The ID embeds an ISO-style YYYYMMDDTHHMMSSZ stamp + 8 hex chars.
        # Avoid pinning the exact stamp; assert structural shape.
        suffix = plt_id.removeprefix("plt-pipeline-")
        timestamp_part, _, token_part = suffix.partition("-")
        assert len(timestamp_part) == len("YYYYMMDDTHHMMSSZ")
        assert timestamp_part.endswith("Z")
        assert len(token_part) == 8 and all(c in "0123456789abcdef" for c in token_part)

    async def test_row_captures_runtime_provenance_fields(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        with _patch_subprocess():
            await record_process_lifetime(
                session_factory=session_factory,
                process_role="monitor",
                archive_root=tmp_path,
            )

        row = await _read_single_row(session_factory)
        # Vocabulary + non-empty load-bearing fields.
        assert row.process_role == "monitor"
        assert row.process_start_at.endswith("Z")
        assert row.process_pid > 0
        assert row.hostname  # non-empty
        assert row.git_sha == "a" * 40
        assert row.git_branch == "main"
        assert row.git_dirty == 0  # clean working tree
        assert row.python_version  # non-empty
        # SHA-256 hash of pip freeze output is 64 hex chars.
        assert len(row.pip_freeze_hash) == 64
        assert all(c in "0123456789abcdef" for c in row.pip_freeze_hash)
        # SDK versions filled in from the runtime; not pinning exact values
        # because they change with dependency bumps.
        assert row.anthropic_sdk_version
        assert row.claude_agent_sdk_version
        assert row.os_release

    async def test_writes_pip_freeze_snapshot_at_documented_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        with _patch_subprocess():
            plt_id = await record_process_lifetime(
                session_factory=session_factory,
                process_role="pipeline",
                archive_root=tmp_path,
            )

        expected = tmp_path / "process_lifetimes" / plt_id / "pip_freeze.txt"
        assert expected.exists()
        row = await _read_single_row(session_factory)
        assert row.pip_freeze_snapshot_path == str(expected)
        # Stub returns "pkg==1.0\n" — confirm the helper wrote that payload.
        assert expected.read_text() == "pkg==1.0\n"

    async def test_dirty_working_tree_persists_as_one(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        def dirty_stub(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[1:] if args and args[0] == "git" else args
            if cmd[:2] == ["status", "--porcelain"]:
                return subprocess.CompletedProcess(
                    args, returncode=0, stdout=" M file.py\n", stderr=""
                )
            return _git_rev_parse_stub(args, **kwargs)

        with _patch_subprocess(stub=dirty_stub):
            await record_process_lifetime(
                session_factory=session_factory,
                process_role="pipeline",
                archive_root=tmp_path,
            )

        row = await _read_single_row(session_factory)
        assert row.git_dirty == 1


class TestRecordProcessLifetimeFailFast:
    async def test_raises_on_non_zero_git_head_exit_and_inserts_no_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        def bad_git_stub(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[1:] if args and args[0] == "git" else args
            if cmd[:2] == ["rev-parse", "HEAD"]:
                raise subprocess.CalledProcessError(returncode=128, cmd=args, stderr="fatal: ...")
            return _git_rev_parse_stub(args, **kwargs)

        with _patch_subprocess(stub=bad_git_stub), pytest.raises(subprocess.CalledProcessError):
            await record_process_lifetime(
                session_factory=session_factory,
                process_role="pipeline",
                archive_root=tmp_path,
            )

        # No row inserted.
        async with session_factory() as sess:
            result = await sess.execute(select(ProcessLifetimeRow))
            assert list(result.scalars()) == []
