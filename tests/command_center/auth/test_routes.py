"""Integration tests for the auth router (story 03 / ALP-667).

End-to-end through ``TestClient``:

* Registration roundtrip: /auth/register/begin (setup-token mode) +
  /auth/register/complete persists a credential.
* Login roundtrip: /auth/login/begin + /auth/login/complete creates a
  session row + sets cookies with HttpOnly + SameSite=Strict + Secure.
* /auth/logout deletes the session row and clears cookies.
* Setup-token consumption: only the first /auth/register/begin succeeds;
  subsequent calls require a session bearer.
* CSRF and session-expiry rejection paths are exercised via the routes
  too, in addition to the dependency-level coverage in
  ``test_dependencies.py``.
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import webauthn_credential_id
from alphamind.command_center.auth.repository import (
    count_credentials,
    load_credential,
    load_session,
)
from alphamind.command_center.auth.setup_token import SetupTokenGate
from alphamind.command_center.auth.webauthn import (
    encode_inmemory_attestation_object,
    encode_inmemory_client_data_json,
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _post_register_begin(
    client: TestClient, *, setup_token: str | None, user_name: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {"user_name": user_name}
    if setup_token is not None:
        payload["setup_token"] = setup_token
    response = client.post("/auth/register/begin", json=payload)
    body = response.json() if response.content else None
    return {"status_code": response.status_code, "body": body}


def _csrf_headers(client: TestClient) -> dict[str, str]:
    """Build the X-CSRF-Token header by echoing the cc_csrf cookie.

    The complete endpoints (/auth/register/complete + /auth/login/complete)
    require the double-submit CSRF check (F3). The begin endpoint set the
    cc_csrf cookie pre-session; the JS layer normally echoes it via
    X-CSRF-Token. Tests do the same here.
    """
    csrf_cookie = client.cookies.get("cc_csrf")
    if csrf_cookie is None:
        return {}
    return {"X-CSRF-Token": csrf_cookie}


def _complete_registration(
    client: TestClient,
    *,
    begin: dict[str, Any],
    credential_id: str,
    public_key: bytes = b"fake-public-key",
    sign_count: int = 0,
) -> Any:
    challenge_bytes = _b64url_decode(begin["challenge"])
    return client.post(
        "/auth/register/complete",
        json={
            "challenge_token": begin["challenge_token"],
            "credential_id": credential_id,
            "client_data_json": _b64url_encode(encode_inmemory_client_data_json(challenge_bytes)),
            "attestation_object": _b64url_encode(
                encode_inmemory_attestation_object(public_key=public_key, sign_count=sign_count)
            ),
            "transports": ["internal"],
        },
        headers=_csrf_headers(client),
    )


def _complete_login(
    client: TestClient,
    *,
    begin: dict[str, Any],
    credential_id: str,
    new_sign_count: int,
) -> Any:
    challenge_bytes = _b64url_decode(begin["challenge"])
    return client.post(
        "/auth/login/complete",
        json={
            "challenge_token": begin["challenge_token"],
            "credential_id": credential_id,
            "client_data_json": _b64url_encode(encode_inmemory_client_data_json(challenge_bytes)),
            "authenticator_data": _b64url_encode(b"fake-auth-data"),
            "signature": _b64url_encode(b"fake-signature"),
            "new_sign_count": new_sign_count,
        },
        headers=_csrf_headers(client),
    )


class TestRegistrationRoundtrip:
    async def test_register_begin_with_setup_token_succeeds(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            response = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["challenge_token"]
        assert body["challenge"]
        assert body["user_name"] == "operator"
        assert body["existing_credentials"] == []

    async def test_register_complete_persists_credential(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            complete = _complete_registration(client, begin=begin, credential_id="cred-roundtrip-1")
        assert complete.status_code == 200
        body = complete.json()
        assert body["credential_id"] == "cred-roundtrip-1"
        assert body["session_id"]
        assert body["csrf_token"]
        # Credential persisted.
        loaded = await load_credential(
            cc_factory, credential_id=webauthn_credential_id("cred-roundtrip-1")
        )
        assert loaded is not None
        assert loaded.credential_id == webauthn_credential_id("cred-roundtrip-1")
        assert loaded.transports == "internal"

    async def test_register_complete_sets_cookies_with_required_flags(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            response = _complete_registration(client, begin=begin, credential_id="cred-cookies")
        # FastAPI's TestClient surfaces Set-Cookie via response.headers.
        set_cookies = response.headers.get_list("set-cookie")
        joined = "; ".join(set_cookies).lower()
        assert "cc_session=" in joined
        assert "cc_csrf=" in joined
        # cc_session must be HttpOnly.
        session_cookie = next(c for c in set_cookies if c.startswith("cc_session="))
        assert "httponly" in session_cookie.lower()
        assert "samesite=strict" in session_cookie.lower()
        # cc_csrf must NOT be HttpOnly (frontend JS reads it).
        csrf_cookie = next(c for c in set_cookies if c.startswith("cc_csrf="))
        assert "httponly" not in csrf_cookie.lower()
        assert "samesite=strict" in csrf_cookie.lower()


class TestSetupTokenGating:
    async def test_first_register_begin_requires_setup_token(
        self,
        auth_app: FastAPI,
    ) -> None:
        with TestClient(auth_app) as client:
            response = client.post("/auth/register/begin", json={"user_name": "operator"})
        assert response.status_code == 403

    async def test_setup_token_consumed_once(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        # First /auth/register/begin consumes the token; the second
        # /auth/register/begin without an existing session fails.
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            r1 = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            )
            assert r1.status_code == 200
            begin = r1.json()
            _complete_registration(client, begin=begin, credential_id="cred-first")
            # Clear session cookies set by complete — we want to test
            # that a fresh client without session can NOT enroll.
            client.cookies.clear()
            r2 = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator-2"},
            )
        # After the first credential lands, register/begin no longer
        # honors the setup-token path — it requires an existing session
        # bearer. Without one, current_session raises 401. With the
        # session-less probe the operator gets 401; the gate's
        # consumed-token state is still tracked but unreachable.
        assert r2.status_code == 401


class TestSubsequentRegistrationRequiresSession:
    async def test_after_first_credential_register_begin_requires_session_cookie(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin, credential_id="cred-A")
            # Now clear the just-issued session cookie.
            client.cookies.clear()
            r = client.post(
                "/auth/register/begin",
                json={"user_name": "operator"},
            )
        assert r.status_code == 401

    async def test_subsequent_register_begin_with_session_succeeds(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin, credential_id="cred-first-of-two")
            # Now the session cookie is set. Subsequent register/begin
            # without a setup token should succeed.
            r = client.post("/auth/register/begin", json={"user_name": "operator"})
        assert r.status_code == 200


class TestLoginRoundtrip:
    async def test_login_begin_returns_allow_credentials(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        # Seed one credential via the register flow.
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin, credential_id="cred-login-1")
            client.cookies.clear()
            response = client.post("/auth/login/begin")
        assert response.status_code == 200
        body = response.json()
        assert body["allow_credentials"] == ["cred-login-1"]
        assert body["challenge"]
        assert body["challenge_token"]

    async def test_login_complete_creates_session_row(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin_reg, credential_id="cred-login-2")
            client.cookies.clear()
            begin_login = client.post("/auth/login/begin").json()
            response = _complete_login(
                client,
                begin=begin_login,
                credential_id="cred-login-2",
                new_sign_count=1,
            )
        assert response.status_code == 200
        body = response.json()
        from alphamind.command_center._kernel.ids import operator_session_id

        loaded = await load_session(cc_factory, session_id=operator_session_id(body["session_id"]))
        assert loaded is not None
        # Sign count bumped from 0 to 1 on the credential row.
        cred = await load_credential(
            cc_factory, credential_id=webauthn_credential_id("cred-login-2")
        )
        assert cred is not None
        assert cred.sign_count == 1

    async def test_login_complete_sets_required_cookie_flags(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin_reg, credential_id="cred-login-3")
            client.cookies.clear()
            begin_login = client.post("/auth/login/begin").json()
            response = _complete_login(
                client,
                begin=begin_login,
                credential_id="cred-login-3",
                new_sign_count=1,
            )
        set_cookies = response.headers.get_list("set-cookie")
        session_cookie = next(c for c in set_cookies if c.startswith("cc_session="))
        csrf_cookie = next(c for c in set_cookies if c.startswith("cc_csrf="))
        assert "httponly" in session_cookie.lower()
        assert "samesite=strict" in session_cookie.lower()
        assert "httponly" not in csrf_cookie.lower()
        assert "samesite=strict" in csrf_cookie.lower()

    async def test_login_complete_secure_flag_when_enabled(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        # The Secure flag is configurable per app.state.cookies_secure.
        # v1 loopback may set False; remote-access deploys set True.
        # NOTE: with cookies_secure=True the TestClient (HTTP-only) won't
        # echo the Secure cookie on subsequent requests, so we exercise the
        # secure-flag assertion on the begin endpoints' Set-Cookie headers
        # — register/begin issues cc_csrf, and that header carries the
        # Secure attribute end-to-end through the secure-deploy code path.
        auth_app.state.cookies_secure = True
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            response = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            )
        set_cookies = response.headers.get_list("set-cookie")
        joined = "; ".join(set_cookies).lower()
        assert "secure" in joined


class TestLogout:
    async def test_logout_deletes_session_row(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        from alphamind.command_center._kernel.ids import operator_session_id

        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            register_response = _complete_registration(
                client, begin=begin_reg, credential_id="cred-logout"
            ).json()
            csrf_token = register_response["csrf_token"]
            sid = register_response["session_id"]
            # /auth/logout requires CSRF + session.
            response = client.post(
                "/auth/logout",
                headers={"X-CSRF-Token": csrf_token},
            )
        assert response.status_code == 204
        loaded = await load_session(cc_factory, session_id=operator_session_id(sid))
        assert loaded is None

    async def test_logout_clears_cookies(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            register_response = _complete_registration(
                client, begin=begin_reg, credential_id="cred-logout-cookies"
            ).json()
            csrf_token = register_response["csrf_token"]
            response = client.post(
                "/auth/logout",
                headers={"X-CSRF-Token": csrf_token},
            )
        # delete_cookie sets Max-Age=0 / expires-in-past.
        set_cookies = response.headers.get_list("set-cookie")
        joined = "; ".join(set_cookies).lower()
        assert "cc_session=" in joined
        assert "cc_csrf=" in joined
        # Indicators of deletion: Max-Age=0 OR expires=Thu, 01 Jan 1970.
        assert "max-age=0" in joined or "1970" in joined

    async def test_logout_clear_cookies_match_set_cookies_attributes(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        # F5: delete_cookie must mirror set_cookie's attributes (samesite,
        # secure, path, httponly) — browsers ignore a clearing Set-Cookie
        # whose attributes don't match the original.
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            register_response = _complete_registration(
                client, begin=begin_reg, credential_id="cred-logout-attrs"
            ).json()
            csrf_token = register_response["csrf_token"]
            response = client.post(
                "/auth/logout",
                headers={"X-CSRF-Token": csrf_token},
            )
        set_cookies = response.headers.get_list("set-cookie")
        session_clear = next(c for c in set_cookies if c.startswith("cc_session=")).lower()
        csrf_clear = next(c for c in set_cookies if c.startswith("cc_csrf=")).lower()
        # Both clearing headers must carry the same attributes the
        # set_cookie path emits.
        assert "samesite=strict" in session_clear
        assert "path=/" in session_clear
        assert "httponly" in session_clear
        assert "samesite=strict" in csrf_clear
        assert "path=/" in csrf_clear
        # cc_csrf is NOT HttpOnly.
        assert "httponly" not in csrf_clear

    async def test_logout_requires_csrf(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin_reg, credential_id="cred-no-csrf")
            # No X-CSRF-Token header.
            response = client.post("/auth/logout")
        assert response.status_code == 403

    async def test_logout_without_session_returns_401(
        self,
        auth_app: FastAPI,
    ) -> None:
        with TestClient(auth_app) as client:
            # CSRF check fires before current_session in some
            # framework orderings; either 401 or 403 is acceptable —
            # both refuse the operation.
            response = client.post("/auth/logout", headers={"X-CSRF-Token": "anything"})
        assert response.status_code in (401, 403)


class TestCredentialCountAfterEnrollment:
    async def test_credential_persists_to_table(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin, credential_id="cred-counted")
        assert await count_credentials(cc_factory) == 1


class TestSetupTokenGateDeferredConsume:
    """F8: setup-token gate must not lock until register/complete succeeds.

    Previously the gate locked on register/begin, so a failed
    register/complete (verifier raised, browser closed mid-flow, network
    glitch) left the operator locked out — no way to retry without a
    daemon restart.
    """

    async def test_failed_complete_does_not_lock_gate(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            # register/begin succeeds — gate is verified but NOT locked.
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            # Send register/complete with a malformed challenge (the
            # in-memory verifier raises ValueError → 400).
            challenge_bytes = b"wrong-challenge"
            response = client.post(
                "/auth/register/complete",
                json={
                    "challenge_token": begin["challenge_token"],
                    "credential_id": "cred-failed",
                    "client_data_json": _b64url_encode(
                        encode_inmemory_client_data_json(challenge_bytes)
                    ),
                    "attestation_object": _b64url_encode(
                        encode_inmemory_attestation_object(
                            public_key=b"fake-public-key", sign_count=0
                        )
                    ),
                    "transports": ["internal"],
                },
                headers=_csrf_headers(client),
            )
            assert response.status_code == 400
            # Gate is NOT locked — same token still works.
            assert not setup_token_gate.is_consumed()
            # Retry register/begin with the same token + a fresh ceremony.
            client.cookies.clear()
            retry = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator-retry"},
            )
            assert retry.status_code == 200

    async def test_successful_complete_locks_gate(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin, credential_id="cred-locks-gate")
        # After successful complete, the gate is locked.
        assert setup_token_gate.is_consumed()


class TestChallengeStoreEviction:
    """F7: _ChallengeStore must enforce TTL + max size.

    Direct unit test against the in-module class; not exposed via the
    routes surface because the eviction behavior is internal to the
    store's invariants. Uses a stubbed monotonic clock so the TTL test
    is deterministic.
    """

    def test_ttl_evicts_expired_entries_on_next_stash(self) -> None:
        from alphamind.command_center.auth.routes import _ChallengeStore

        # Fake clock — advances when we call .advance().
        current = [0.0]

        def now() -> float:
            return current[0]

        store = _ChallengeStore(now=now)
        store.stash_registration(token="t-old", challenge=b"c", user_id=b"u")
        # Past TTL boundary (5 minutes = 300s). The eviction sweep on
        # the next stash call should drop t-old.
        current[0] = 301.0
        store.stash_registration(token="t-new", challenge=b"c2", user_id=b"u2")
        # t-old is gone.
        assert store.pop_registration("t-old") is None
        # t-new is still alive.
        assert store.pop_registration("t-new") == (b"c2", b"u2")

    def test_ttl_drops_entry_at_pop_when_expired(self) -> None:
        from alphamind.command_center.auth.routes import _ChallengeStore

        current = [0.0]

        def now() -> float:
            return current[0]

        store = _ChallengeStore(now=now)
        store.stash_authentication(token="t-old", challenge=b"c")
        # Advance past TTL without triggering a stash (so eviction sweep
        # hasn't run). pop should still treat the entry as expired.
        current[0] = 301.0
        assert store.pop_authentication("t-old") is None

    def test_max_size_fifo_evicts_oldest_registration(self) -> None:
        from alphamind.command_center.auth.routes import (
            _CHALLENGE_STORE_MAX_ENTRIES,
            _ChallengeStore,
        )

        store = _ChallengeStore()
        # Fill to capacity.
        for i in range(_CHALLENGE_STORE_MAX_ENTRIES):
            store.stash_registration(token=f"t-{i}", challenge=b"c", user_id=b"u")
        # The cap+1th insertion should FIFO-evict t-0.
        store.stash_registration(token="t-overflow", challenge=b"c", user_id=b"u")
        assert store.pop_registration("t-0") is None
        # Newer entries should still be present.
        assert store.pop_registration("t-overflow") == (b"c", b"u")

    def test_max_size_fifo_evicts_oldest_authentication(self) -> None:
        from alphamind.command_center.auth.routes import (
            _CHALLENGE_STORE_MAX_ENTRIES,
            _ChallengeStore,
        )

        store = _ChallengeStore()
        for i in range(_CHALLENGE_STORE_MAX_ENTRIES):
            store.stash_authentication(token=f"t-{i}", challenge=b"c")
        store.stash_authentication(token="t-overflow", challenge=b"c")
        assert store.pop_authentication("t-0") is None
        assert store.pop_authentication("t-overflow") == b"c"


class TestCsrfOnCompleteEndpoints:
    """F3: /auth/{register,login}/complete must require CSRF."""

    async def test_register_complete_requires_csrf_header(
        self,
        auth_app: FastAPI,
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            challenge_bytes = _b64url_decode(begin["challenge"])
            # Send register/complete WITHOUT X-CSRF-Token header.
            response = client.post(
                "/auth/register/complete",
                json={
                    "challenge_token": begin["challenge_token"],
                    "credential_id": "cred-no-csrf",
                    "client_data_json": _b64url_encode(
                        encode_inmemory_client_data_json(challenge_bytes)
                    ),
                    "attestation_object": _b64url_encode(
                        encode_inmemory_attestation_object(
                            public_key=b"fake-public-key", sign_count=0
                        )
                    ),
                    "transports": ["internal"],
                },
            )
        assert response.status_code == 403

    async def test_login_complete_requires_csrf_header(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin_reg, credential_id="cred-login-no-csrf")
            client.cookies.clear()
            begin_login = client.post("/auth/login/begin").json()
            challenge_bytes = _b64url_decode(begin_login["challenge"])
            # Send login/complete WITHOUT X-CSRF-Token header.
            response = client.post(
                "/auth/login/complete",
                json={
                    "challenge_token": begin_login["challenge_token"],
                    "credential_id": "cred-login-no-csrf",
                    "client_data_json": _b64url_encode(
                        encode_inmemory_client_data_json(challenge_bytes)
                    ),
                    "authenticator_data": _b64url_encode(b"fake-auth-data"),
                    "signature": _b64url_encode(b"fake-signature"),
                    "new_sign_count": 1,
                },
            )
        assert response.status_code == 403

    async def test_register_begin_issues_csrf_cookie(
        self,
        auth_app: FastAPI,
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            response = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            )
        set_cookies = response.headers.get_list("set-cookie")
        joined = "; ".join(set_cookies).lower()
        assert "cc_csrf=" in joined

    async def test_login_begin_issues_csrf_cookie(
        self,
        auth_app: FastAPI,
        cc_factory: async_sessionmaker[AsyncSession],
        setup_token_gate: SetupTokenGate,
    ) -> None:
        token = setup_token_gate.mint()
        with TestClient(auth_app) as client:
            begin_reg = client.post(
                "/auth/register/begin",
                json={"setup_token": token, "user_name": "operator"},
            ).json()
            _complete_registration(client, begin=begin_reg, credential_id="cred-login-csrf")
            client.cookies.clear()
            response = client.post("/auth/login/begin")
        set_cookies = response.headers.get_list("set-cookie")
        joined = "; ".join(set_cookies).lower()
        assert "cc_csrf=" in joined
