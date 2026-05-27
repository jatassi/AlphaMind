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
    _capture_pip_freeze,
    record_process_lifetime,
)
from alphamind.state.tables.process_lifetimes import (
    ProcessLifetimeRow,
)


class _StubDistribution:
    """Stand-in for ``importlib.metadata.Distribution`` exposing name + version."""

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


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
    cmd = args[1:] if args and args[0] == "git" else args
    if cmd[:2] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="a" * 40 + "\n", stderr="")
    if cmd[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="main\n", stderr="")
    if cmd[:2] == ["status", "--porcelain"]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected subprocess invocation in test: {args!r}")


_STUB_DISTRIBUTIONS: tuple[_StubDistribution, ...] = (
    _StubDistribution("pkg-b", "2.0"),
    _StubDistribution("pkg-a", "1.0"),
)


def _patch_subprocess(stub: Any = _git_rev_parse_stub) -> Any:
    """Patch subprocess.run inside the helper module to return a stub."""
    return patch(
        "alphamind.state.process_lifetime.subprocess.run",
        side_effect=lambda args, **kwargs: stub(args, **kwargs),
    )


def _patch_platform_platform(value: str = "Linux-stub-test") -> Any:
    """Patch ``platform.platform()`` inside the helper module.

    On macOS, ``platform.platform()`` shells out to ``uname -p`` (and on
    some configurations ``file -b``) to determine processor type. Those
    extra subprocess calls trip the ``_git_rev_parse_stub`` guard above.
    On the Windows CI runner and on Linux, ``platform.platform()`` is
    pure-Python and doesn't shell out. Patching it directly keeps the
    test platform-agnostic without expanding the subprocess stub to
    cover every Mac-internal probe.
    """
    return patch("alphamind.state.process_lifetime.platform.platform", return_value=value)


def _patch_distributions(distributions: tuple[_StubDistribution, ...] = _STUB_DISTRIBUTIONS) -> Any:
    """Patch ``importlib.metadata.distributions`` inside the helper module."""
    return patch(
        "alphamind.state.process_lifetime.importlib.metadata.distributions",
        return_value=distributions,
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
        with _patch_subprocess(), _patch_distributions(), _patch_platform_platform():
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
        with _patch_subprocess(), _patch_distributions(), _patch_platform_platform():
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
        with _patch_subprocess(), _patch_distributions(), _patch_platform_platform():
            plt_id = await record_process_lifetime(
                session_factory=session_factory,
                process_role="pipeline",
                archive_root=tmp_path,
            )

        expected = tmp_path / "process_lifetimes" / plt_id / "pip_freeze.txt"
        assert expected.exists()
        row = await _read_single_row(session_factory)
        assert row.pip_freeze_snapshot_path == str(expected)
        # Stub distributions render as a pip-freeze-format snapshot, sorted.
        assert expected.read_text() == "pkg-a==1.0\npkg-b==2.0\n"

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

        with (
            _patch_subprocess(stub=dirty_stub),
            _patch_distributions(),
            _patch_platform_platform(),
        ):
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

        with (
            _patch_subprocess(stub=bad_git_stub),
            _patch_distributions(),
            _patch_platform_platform(),
            pytest.raises(subprocess.CalledProcessError),
        ):
            await record_process_lifetime(
                session_factory=session_factory,
                process_role="pipeline",
                archive_root=tmp_path,
            )

        # No row inserted.
        async with session_factory() as sess:
            result = await sess.execute(select(ProcessLifetimeRow))
            assert list(result.scalars()) == []


class TestRecordProcessLifetimeEventLoopProgress:
    """ALP-715 review F2(b): blocking provenance must run off the event loop."""

    async def test_event_loop_progresses_during_blocking_provenance(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Blocking subprocess + file-write work runs in a worker thread.

        We give one of the patched subprocess calls a deliberately slow
        sleep and verify a concurrent ``asyncio.sleep`` makes progress in
        parallel. If the gathered provenance work were inline on the event
        loop, the concurrent sleep could not complete first.
        """
        import asyncio
        import threading
        import time as _time

        loop_thread_id = threading.get_ident()
        provenance_thread_ids: list[int] = []

        def _slow_git_stub(
            args: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            provenance_thread_ids.append(threading.get_ident())
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                # Block long enough that a concurrent asyncio.sleep(0.01)
                # would still complete first if the call is off the loop.
                _time.sleep(0.05)
            return _git_rev_parse_stub(args, **kwargs)

        async def _record() -> str:
            with (
                _patch_subprocess(stub=_slow_git_stub),
                _patch_distributions(),
                _patch_platform_platform(),
            ):
                return await record_process_lifetime(
                    session_factory=session_factory,
                    process_role="monitor",
                    archive_root=tmp_path,
                )

        plt_id, _ = await asyncio.gather(_record(), asyncio.sleep(0.01))
        assert plt_id.startswith("plt-monitor-")
        assert provenance_thread_ids, "subprocess stub never ran"
        # Every provenance subprocess invocation must have run in a worker
        # thread — not the event loop's thread.
        for tid in provenance_thread_ids:
            assert tid != loop_thread_id, (
                "blocking provenance must run off the event loop; "
                f"observed on loop thread id={loop_thread_id}"
            )


class TestCapturePipFreeze:
    """Direct tests for ``_capture_pip_freeze``'s importlib.metadata path."""

    def test_returns_sorted_name_eq_version_lines(self) -> None:
        with _patch_distributions(
            (
                _StubDistribution("Zeta", "9.0"),
                _StubDistribution("alpha", "1.2.3"),
                _StubDistribution("Beta", "0.1"),
            )
        ):
            assert _capture_pip_freeze() == "alpha==1.2.3\nBeta==0.1\nZeta==9.0\n"

    def test_empty_environment_returns_empty_string(self) -> None:
        with _patch_distributions(()):
            assert _capture_pip_freeze() == ""

    def test_uses_no_subprocess_call(self) -> None:
        """The replacement must be hermetic — no shell-out to ``pip freeze``."""

        def fail_if_called(*args: Any, **kwargs: Any) -> None:
            raise AssertionError(f"unexpected subprocess.run: args={args!r}, kwargs={kwargs!r}")

        with (
            patch("alphamind.state.process_lifetime.subprocess.run", side_effect=fail_if_called),
            _patch_distributions(),
        ):
            assert _capture_pip_freeze() == "pkg-a==1.0\npkg-b==2.0\n"

    def test_dedupes_shadowed_packages_keeping_first_seen(self) -> None:
        """Editable install + stale site-packages copy yields one row per name."""
        with _patch_distributions(
            (
                _StubDistribution("alpha", "2.0"),  # first wins
                _StubDistribution("beta", "0.1"),
                _StubDistribution("alpha", "1.0"),  # shadowed copy
            )
        ):
            assert _capture_pip_freeze() == "alpha==2.0\nbeta==0.1\n"
