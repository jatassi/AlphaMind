"""Shared test fixtures for the auth subpackage.

Builds a minimal FastAPI test app wired with:

* In-memory cc_writer session factory (per-test fresh DB).
* In-memory ``InMemoryWebauthnVerifier``.
* In-memory :class:`SetupTokenGate`.
* A fixed signing secret + a configurable :class:`Clock`.

Subsequent tests exercise the dependencies + routes through this app's
``TestClient``. Keeps each test focused on auth behavior without
dragging in the full command-center composition root.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center.auth.setup_token import SetupTokenGate
from alphamind.command_center.auth.webauthn import (
    InMemoryWebauthnVerifier,
    WebauthnVerifier,
)
from alphamind.command_center.config import (
    CsrfConfig,
    SecurityConfig,
    SessionConfig,
    WebauthnConfig,
)
from alphamind.command_center.persistence.session import (
    build_cc_writer_session_factory,
)
from alphamind.command_center.persistence.tables import CommandCenterBase


_SECRET = b"test-secret-key-32-bytes-long!!!!"


@pytest.fixture
def security_config() -> SecurityConfig:
    return SecurityConfig(
        session=SessionConfig(duration_hours=12, cookie_name="cc_session"),
        csrf=CsrfConfig(cookie_name="cc_csrf"),
        webauthn=WebauthnConfig(
            relying_party_id="localhost",
            relying_party_name="AlphaMind Command Center",
        ),
    )


@pytest.fixture
async def cc_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = build_cc_writer_session_factory(":memory:")
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


class FrozenClock:
    """Adjustable clock for session-expiry tests.

    Tests advance the clock by calling :meth:`advance`; :meth:`now`
    returns the current value. Stored as a callable on ``app.state``
    so the dependency layer reads consistent time.
    """

    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def now(self) -> datetime:
        return self._now

    def advance(self, *, hours: int = 0, seconds: int = 0) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(hours=hours, seconds=seconds)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC))


@pytest.fixture
def webauthn_verifier() -> WebauthnVerifier:
    return InMemoryWebauthnVerifier(relying_party_id="localhost")


@pytest.fixture
def setup_token_gate() -> SetupTokenGate:
    return SetupTokenGate()


@pytest.fixture
def auth_app(
    security_config: SecurityConfig,
    cc_factory: async_sessionmaker[AsyncSession],
    webauthn_verifier: WebauthnVerifier,
    setup_token_gate: SetupTokenGate,
    clock: FrozenClock,
) -> Iterator[FastAPI]:
    """Build a minimal FastAPI test app wired with the auth router."""
    from alphamind.command_center.auth.routes import build_auth_router

    app = FastAPI()
    app.state.security_config = security_config
    app.state.cc_writer_session_factory = cc_factory
    app.state.webauthn_verifier = webauthn_verifier
    app.state.setup_token_gate = setup_token_gate
    app.state.session_signing_secret = _SECRET
    app.state.clock = clock.now
    app.include_router(build_auth_router())
    yield app


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).parents[3]
