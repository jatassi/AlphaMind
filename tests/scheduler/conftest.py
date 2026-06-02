"""Shared fixtures for the scheduler shard (ALP-793).

Centralizes the duplicated ``async_factory`` (with process_lifetime seed for FKs),
venue env fixtures (env_path, archive_root, db_path), process-lifetime / venue
builders, _VENUE_ENV_KEYS + _write helper, REPO_ROOT / SHIPPED_CONFIG_DIR, and the
autouse engine disposer that was hand-copied across ~10 files (~400 LOC).

Consumers delete their local copies and (where needed) import the helpers via::

    from tests.scheduler.conftest import (
        _make_process_lifetime_record,
        _make_venue_config,
        REPO_ROOT,
        SHIPPED_CONFIG_DIR,
        ...
    )

The ``async_factory`` here seeds a process_lifetime row using the canonical
"proc-driver-1" ID (chosen so driver.py's literal assert continues to match
without editing any assertion).

Specialized variants (e.g. emergency's bootstrap-inv, orchestrator's with_singletons,
debug_e2e's tuple return + debug db name, invocation_build's no-seed) are either
harmlessly superseded by the common (extra rows do not affect behavior) or keep
local overrides / call explicit seeds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config.models.venue import (
    Alpaca,
    AlpacaCredentials,
    SessionHours,
    SessionWindow,
    VenueConfig,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)

REPO_ROOT = Path(__file__).parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"

_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)


def _write_placeholder_env(env_path: Path) -> None:
    env_path.write_text("\n".join(f"{key}=placeholder" for key in _VENUE_ENV_KEYS) + "\n")


def _make_process_lifetime_record(
    process_lifetime_id: str = "proc-driver-1",
) -> ProcessLifetimeRecord:
    """Return a minimal ProcessLifetimeRecord; ID can be overridden by callers."""
    pip_path = f"/tmp/provenance/process_lifetimes/{process_lifetime_id}/pip_freeze.txt"
    return ProcessLifetimeRecord(
        process_lifetime_id=process_lifetime_id,
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path=pip_path,
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _write_placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return the sqlite file path used by the scheduler async_factory fixtures."""
    return tmp_path / "alphamind.db"


@pytest.fixture
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async session factory bound to an initialized SQLite DB.

    Seeds the FK parent ``process_lifetimes`` row (using "proc-driver-1") so
    call sites that invoke insert/build paths can satisfy the foreign-key
    constraint without per-test setup. (Specialized seeds such as bootstrap
    invocation rows for activity_log FKs are the caller's responsibility or
    handled by per-file helpers.)
    """
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    try:
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
            sess.commit()
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


# ---------------------------------------------------------------------------
# Autouse engine disposal (hoisted from orchestrator* modules).
# Sync engines built inside ``_make_context`` (and similar) register here; the
# autouse disposes them after every test so Windows SQLite file handles are
# released before pytest's tmp_path teardown runs.
# ---------------------------------------------------------------------------

_MAKE_CONTEXT_ENGINES: list[Engine] = []


@pytest.fixture(autouse=True)
def _dispose_make_context_engines() -> Iterator[None]:
    yield
    while _MAKE_CONTEXT_ENGINES:
        _MAKE_CONTEXT_ENGINES.pop().dispose()


def _make_venue_config() -> VenueConfig:
    creds = AlpacaCredentials(
        rest_url="https://paper-api.alpaca.markets",
        ws_url="wss://paper-api.alpaca.markets",
        api_key_env="ALPACA_PAPER_KEY",
        api_secret_env="ALPACA_PAPER_SECRET",
    )
    return VenueConfig(
        alpaca=Alpaca(paper=creds, live=creds, rate_limit_per_minute=200),
        session_hours=SessionHours(
            regular=SessionWindow(open="09:30", close="16:00"),
            pre_market=SessionWindow(open="04:00", close="09:30"),
            after_hours=SessionWindow(open="16:00", close="20:00"),
        ),
    )
