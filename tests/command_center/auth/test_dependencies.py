"""Tests for ``command_center.auth.dependencies`` (story 03 / ALP-667).

Exercises the two FastAPI ``Depends`` callables that every ``/api/*``
route added by later stories will gate behind:

* :func:`current_session` — returns the validated :class:`OperatorSessionId`
  or raises ``HTTPException(401)``.
* :func:`csrf_required` — raises ``HTTPException(403)`` if the
  ``X-CSRF-Token`` header doesn't match the ``cc_csrf`` cookie.

Tests use a minimal protected route mounted on the auth-test app +
direct cookie manipulation through ``TestClient.cookies``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import (
    OperatorSessionId,
    operator_session_id,
    webauthn_credential_id,
)
from alphamind.command_center.auth.dependencies import (
    csrf_required,
    current_session,
)
from alphamind.command_center.auth.repository import (
    insert_credential,
    insert_session,
)
from alphamind.command_center.auth.sessions import (
    SessionCookiePayload,
    encode_session_cookie,
    hash_csrf_token,
    mint_csrf_token,
)
from alphamind.command_center.persistence.codecs import (
    OperatorSessionRecord,
    WebauthnCredentialRecord,
)

from tests.command_center.auth.conftest import FrozenClock

_SECRET = b"test-secret-key-32-bytes-long!!!!"


def _attach_protected_route(app: FastAPI) -> None:
    """Mount a tiny protected route on the test app for dependency probing."""

    @app.get("/probe/whoami")
    def whoami(
        sid: Annotated[OperatorSessionId, Depends(current_session)],
    ) -> dict[str, str]:
        return {"session_id": sid}

    @app.post("/probe/mutate")
    def mutate(
        sid: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> dict[str, str]:
        return {"status": "ok", "session_id": sid}


async def _seed_session(
    cc_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    expires_at: str,
    csrf_hash: str,
) -> None:
    await insert_credential(
        cc_factory,
        WebauthnCredentialRecord(
            credential_id=webauthn_credential_id("cred-test"),
            public_key="pk",
            sign_count=0,
            transports="internal",
            created_at="2026-05-26T00:00:00Z",
        ),
    )
    await insert_session(
        cc_factory,
        OperatorSessionRecord(
            session_id=operator_session_id(session_id),
            credential_id=webauthn_credential_id("cred-test"),
            expires_at=expires_at,
            csrf_token_hash=csrf_hash,
            created_at="2026-05-26T00:00:00Z",
        ),
    )


class TestCurrentSession:
    async def test_returns_session_id_for_valid_cookie(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_session(
            cc_factory,
            session_id="sess-1",
            expires_at="2026-05-26T12:00:00Z",
            csrf_hash="any",
        )
        _attach_protected_route(auth_app)
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("sess-1"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=_SECRET,
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            response = client.get("/probe/whoami")
        assert response.status_code == 200
        assert response.json() == {"session_id": "sess-1"}

    async def test_returns_401_when_cookie_absent(
        self, auth_app: FastAPI
    ) -> None:
        _attach_protected_route(auth_app)
        with TestClient(auth_app) as client:
            response = client.get("/probe/whoami")
        assert response.status_code == 401

    async def test_returns_401_when_signature_invalid(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_session(
            cc_factory,
            session_id="sess-1",
            expires_at="2026-05-26T12:00:00Z",
            csrf_hash="any",
        )
        _attach_protected_route(auth_app)
        # Cookie signed with the wrong secret — signature won't validate.
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("sess-1"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=b"wrong-secret-32-bytes-long!!!!!",
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            response = client.get("/probe/whoami")
        assert response.status_code == 401

    async def test_returns_401_when_session_row_missing(
        self,
        auth_app: FastAPI,
    ) -> None:
        # Valid signed cookie but no DB row — a forged-but-unsigned-and-
        # tracked session can't authenticate. The DB is the authoritative
        # state.
        _attach_protected_route(auth_app)
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("forged-sess"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=_SECRET,
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            response = client.get("/probe/whoami")
        assert response.status_code == 401

    async def test_returns_401_when_session_expired(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        clock: FrozenClock,
    ) -> None:
        await _seed_session(
            cc_factory,
            session_id="sess-exp",
            expires_at="2026-05-26T12:00:00Z",
            csrf_hash="any",
        )
        _attach_protected_route(auth_app)
        # Advance the clock past expiry. The clock fixture starts at
        # 2026-05-26T00:00:00; expires_at is +12h. Advance 24h.
        clock.advance(hours=24)
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("sess-exp"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=_SECRET,
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            response = client.get("/probe/whoami")
        assert response.status_code == 401


class TestCsrfRequired:
    async def test_passes_when_header_matches_cookie(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        csrf = mint_csrf_token()
        await _seed_session(
            cc_factory,
            session_id="sess-csrf",
            expires_at="2026-05-26T12:00:00Z",
            csrf_hash=hash_csrf_token(csrf),
        )
        _attach_protected_route(auth_app)
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("sess-csrf"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=_SECRET,
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            client.cookies.set("cc_csrf", csrf)
            response = client.post(
                "/probe/mutate", headers={"X-CSRF-Token": csrf}
            )
        assert response.status_code == 200

    async def test_returns_403_when_header_absent(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        csrf = mint_csrf_token()
        await _seed_session(
            cc_factory,
            session_id="sess-csrf",
            expires_at="2026-05-26T12:00:00Z",
            csrf_hash=hash_csrf_token(csrf),
        )
        _attach_protected_route(auth_app)
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("sess-csrf"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=_SECRET,
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            client.cookies.set("cc_csrf", csrf)
            response = client.post("/probe/mutate")
        assert response.status_code == 403

    async def test_returns_403_when_header_does_not_match_cookie(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        csrf = mint_csrf_token()
        await _seed_session(
            cc_factory,
            session_id="sess-csrf",
            expires_at="2026-05-26T12:00:00Z",
            csrf_hash=hash_csrf_token(csrf),
        )
        _attach_protected_route(auth_app)
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("sess-csrf"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=_SECRET,
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            client.cookies.set("cc_csrf", csrf)
            response = client.post(
                "/probe/mutate", headers={"X-CSRF-Token": "wrong-token"}
            )
        assert response.status_code == 403

    async def test_returns_403_when_cookie_absent(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Even with a session cookie, the csrf path requires the cc_csrf
        # cookie + matching header.
        csrf = mint_csrf_token()
        await _seed_session(
            cc_factory,
            session_id="sess-csrf",
            expires_at="2026-05-26T12:00:00Z",
            csrf_hash=hash_csrf_token(csrf),
        )
        _attach_protected_route(auth_app)
        cookie = encode_session_cookie(
            SessionCookiePayload(
                session_id=operator_session_id("sess-csrf"),
                expires_at="2026-05-26T12:00:00Z",
            ),
            secret=_SECRET,
        )
        with TestClient(auth_app) as client:
            client.cookies.set("cc_session", cookie)
            # cc_csrf cookie absent.
            response = client.post(
                "/probe/mutate", headers={"X-CSRF-Token": csrf}
            )
        assert response.status_code == 403
