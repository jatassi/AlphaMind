"""Shared fixtures for the events subpackage tests.

Builds a minimal FastAPI test app pre-wired with:

* A session-gated test client that can present a valid session cookie.
* An :class:`EventMultiplexer` on ``app.state`` so the route handler
  can fan events without booting the production lifespan.
* A signing secret + clock + session row inserted into the cc_writer
  factory so :func:`current_session` accepts the test cookie.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import (
    operator_session_id,
    webauthn_credential_id,
)
from alphamind.command_center.auth.repository import (
    insert_credential,
    insert_session,
)
from alphamind.command_center.auth.sessions import (
    SessionCookiePayload,
    encode_session_cookie,
)
from alphamind.command_center.config import (
    CsrfConfig,
    SecurityConfig,
    SessionConfig,
    WebauthnConfig,
)
from alphamind.command_center.events.multiplexer import EventMultiplexer
from alphamind.command_center.events.routes import build_events_router
from alphamind.command_center.persistence.codecs import (
    OperatorSessionRecord,
    WebauthnCredentialRecord,
)
from alphamind.command_center.persistence.session import (
    build_cc_writer_session_factory,
)
from alphamind.command_center.persistence.tables import CommandCenterBase

_SECRET = b"test-secret-key-32-bytes-long!!!!"
_SESSION_ID = "sess-test-1"
_CRED_ID = "cred-test"
_EXPIRES_AT = "2099-01-01T00:00:00Z"
_NOW = datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC)


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


@pytest.fixture
def multiplexer() -> EventMultiplexer:
    return EventMultiplexer()


@pytest.fixture
def events_app(
    security_config: SecurityConfig,
    cc_factory: async_sessionmaker[AsyncSession],
    multiplexer: EventMultiplexer,
) -> Iterator[FastAPI]:
    """Minimal FastAPI app wired with the events router.

    Bypasses ``build_app`` so the events test exercises only the
    router + dependency wiring, not the whole command-center
    composition root.
    """
    app = FastAPI()
    app.state.security_config = security_config
    app.state.cc_writer_session_factory = cc_factory
    app.state.session_signing_secret = _SECRET
    app.state.clock = lambda: _NOW
    app.state.event_multiplexer = multiplexer
    # The events route uses a short heartbeat interval in tests so the
    # heartbeat-cadence test runs in milliseconds.
    app.state.event_heartbeat_interval_seconds = 0.05
    app.include_router(build_events_router())
    yield app


@pytest.fixture
async def session_cookie_value(
    cc_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Insert a session row + return the signed cookie value.

    Tests inject this via ``client.cookies.set(cookie_name, value)``
    so :func:`current_session` accepts the request.
    """
    await insert_credential(
        cc_factory,
        WebauthnCredentialRecord(
            credential_id=webauthn_credential_id(_CRED_ID),
            public_key="pk",
            sign_count=0,
            transports="internal",
            created_at="2026-05-26T00:00:00Z",
        ),
    )
    await insert_session(
        cc_factory,
        OperatorSessionRecord(
            session_id=operator_session_id(_SESSION_ID),
            credential_id=webauthn_credential_id(_CRED_ID),
            expires_at=_EXPIRES_AT,
            csrf_token_hash="any",
            created_at="2026-05-26T00:00:00Z",
        ),
    )
    return encode_session_cookie(
        SessionCookiePayload(
            session_id=operator_session_id(_SESSION_ID),
            expires_at=_EXPIRES_AT,
        ),
        secret=_SECRET,
    )
