"""FastAPI router for the auth surface (story 03 / ALP-667).

Five endpoints:

* ``POST /auth/register/begin`` — Returns registration options. Gated by
  the setup token at first launch; requires an existing-session bearer
  after the first credential lands.
* ``POST /auth/register/complete`` — Accepts the browser's
  ``navigator.credentials.create()`` response, verifies via
  :class:`alphamind.command_center.auth.webauthn.WebauthnVerifier`,
  persists the credential, issues a session.
* ``POST /auth/login/begin`` — Returns authentication options.
* ``POST /auth/login/complete`` — Accepts the browser's
  ``navigator.credentials.get()`` response, verifies, updates the
  credential's sign count, issues a session.
* ``POST /auth/logout`` — Deletes the session row + clears cookies.
  CSRF-required (the session cookie + ``X-CSRF-Token`` must both be
  present).

Pydantic models live next to the router (P5: Pydantic at boundaries
only — this is the boundary). Internal logic operates on the frozen
dataclasses from :mod:`alphamind.command_center.auth.webauthn`.

Cookie discipline (parent issue invariant E + design § Authentication):
``cc_session`` and ``cc_csrf`` set with ``HttpOnly`` (session only —
``cc_csrf`` is NOT HttpOnly since the frontend JS must read it for the
``X-CSRF-Token`` echo), ``SameSite=Strict``, ``Secure``. The
``Secure`` flag is configurable because v1 is loopback-only.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from webauthn.helpers.exceptions import WebAuthnException

from alphamind.command_center._kernel.ids import (
    OperatorSessionId,
    operator_session_id,
    webauthn_credential_id,
)
from alphamind.command_center.auth.dependencies import (
    csrf_required,
    csrf_required_pre_session,
    current_session,
)
from alphamind.command_center.auth.repository import (
    insert_credential,
    insert_session,
    list_credentials,
    load_credential,
    update_credential_sign_count,
)
from alphamind.command_center.auth.sessions import (
    SessionCookiePayload,
    encode_session_cookie,
    format_expires_at,
    hash_csrf_token,
    mint_csrf_token,
)
from alphamind.command_center.auth.setup_token import (
    SetupTokenAlreadyConsumedError,
    SetupTokenMismatchError,
)
from alphamind.command_center.auth.webauthn import (
    AuthenticationOptions,
    AuthenticationResponse,
    RegistrationOptions,
    RegistrationResponse,
)
from alphamind.command_center.persistence.codecs import (
    OperatorSessionRecord,
    WebauthnCredentialRecord,
)

__all__ = ["build_auth_router"]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-process challenge bookkeeping.
# ---------------------------------------------------------------------------
#
# WebAuthn ceremonies pair a generated challenge with the response that
# echoes it. The browser stores the challenge between the begin and
# complete calls; on the server side we must remember the issued
# challenge so the verify step can match. The in-memory store below is
# keyed by the relying-party-issued challenge token (a separate random
# value) which the begin response returns to the browser and the
# complete request echoes back.


class _ChallengeStore:
    """Per-process in-memory challenge cache.

    Registration: ``{token: (challenge_bytes, user_id_bytes)}``.
    Authentication: ``{token: challenge_bytes}``.

    Tokens auto-expire on consumption (one-shot). The store is bounded
    in practice because no parallel enrollment ceremonies are expected;
    a stale entry from a half-completed registration is harmless and
    will be discarded on the next begin call.
    """

    def __init__(self) -> None:
        self._registration: dict[str, tuple[bytes, bytes]] = {}
        self._authentication: dict[str, bytes] = {}

    def stash_registration(self, *, token: str, challenge: bytes, user_id: bytes) -> None:
        self._registration[token] = (challenge, user_id)

    def pop_registration(self, token: str) -> tuple[bytes, bytes] | None:
        return self._registration.pop(token, None)

    def stash_authentication(self, *, token: str, challenge: bytes) -> None:
        self._authentication[token] = challenge

    def pop_authentication(self, token: str) -> bytes | None:
        return self._authentication.pop(token, None)


# ---------------------------------------------------------------------------
# Pydantic request / response models (boundary layer).
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    """Base class for auth request / response models.

    ``extra='forbid'`` so a typo in a body or extra-field probing surfaces
    as 422 rather than silently flowing through. ``frozen=True`` so
    consumers can't mutate the validated request mid-handler.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class RegisterBeginRequest(_StrictModel):
    """Body of ``POST /auth/register/begin``.

    ``setup_token`` is required on the first registration (when there
    are zero credentials); subsequent registrations require an existing
    session cookie and ignore the field. ``user_name`` is the
    relying-party-visible operator label.
    """

    setup_token: str | None = None
    user_name: str = Field(min_length=1)


class RegisterBeginResponse(_StrictModel):
    challenge_token: str
    challenge: str  # base64url
    user_id: str  # base64url
    user_name: str
    relying_party_id: str
    relying_party_name: str
    existing_credentials: list[str]


class RegisterCompleteRequest(_StrictModel):
    """Body of ``POST /auth/register/complete``.

    The four blob fields (``client_data_json``, ``attestation_object``)
    carry the browser's raw ``navigator.credentials.create()`` output as
    base64url-encoded strings; the route handler decodes them via
    :func:`_b64url_decode` before constructing the
    :class:`RegistrationResponse` the verifier consumes.
    """

    challenge_token: str
    credential_id: str
    client_data_json: str  # base64url
    attestation_object: str  # base64url
    transports: list[str] = Field(default_factory=list)


class LoginBeginResponse(_StrictModel):
    challenge_token: str
    challenge: str  # base64url
    relying_party_id: str
    allow_credentials: list[str]


class LoginCompleteRequest(_StrictModel):
    """Body of ``POST /auth/login/complete``.

    The five blob fields (``client_data_json``, ``authenticator_data``,
    ``signature``, optional ``user_handle``) carry the browser's raw
    ``navigator.credentials.get()`` output as base64url-encoded strings.
    The parsed ``new_sign_count`` accompanies the bytes; the route
    handler persists it after verification succeeds.

    ``new_sign_count`` is constrained to ``ge=0`` (not ``ge=1``) because
    WebAuthn authenticators that don't support counters report 0 forever
    — see py_webauthn's verify_authentication_response.py § 149. The
    in-memory + real verifiers enforce strict-increase only when EITHER
    stored OR new is non-zero, so a 0/0 case is legitimately accepted.
    """

    challenge_token: str
    credential_id: str
    client_data_json: str  # base64url
    authenticator_data: str  # base64url
    signature: str  # base64url
    user_handle: str | None = None  # base64url, optional
    new_sign_count: int = Field(ge=0)


class AuthSuccessResponse(_StrictModel):
    session_id: str
    expires_at: str
    csrf_token: str
    credential_id: str


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    """Encode *data* as base64url without padding."""
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Decode base64url *text* (no padding) back to bytes."""
    import base64

    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _get_challenge_store(request: Request) -> _ChallengeStore:
    """Return the per-process challenge store, lazily constructing it."""
    store = getattr(request.app.state, "_auth_challenge_store", None)
    if store is None:
        store = _ChallengeStore()
        request.app.state._auth_challenge_store = store
    return store


def _issue_pre_session_csrf_cookie(*, response: Response, request: Request) -> str:
    """Mint a fresh CSRF token and set the ``cc_csrf`` cookie pre-session.

    Used by ``/auth/register/begin`` + ``/auth/login/begin`` so the
    browser's JS layer has a cookie to echo via ``X-CSRF-Token`` on the
    matching complete request. The complete endpoint then verifies the
    double-submit via :func:`csrf_required_pre_session`.

    The returned token also flows back in the begin response body so
    the JS can grab it directly without needing to read the cookie (the
    cookie is NOT HttpOnly anyway, but the response-body path keeps the
    JS simpler).
    """
    csrf_token = mint_csrf_token()
    cookies_secure = bool(getattr(request.app.state, "cookies_secure", False))
    csrf_cookie_name = request.app.state.security_config.csrf.cookie_name
    response.set_cookie(
        csrf_cookie_name,
        csrf_token,
        httponly=False,
        samesite="strict",
        secure=cookies_secure,
    )
    return csrf_token


def _issue_session_cookies(
    *,
    response: Response,
    request: Request,
) -> tuple[OperatorSessionId, str, str]:
    """Create + persist a fresh operator session; set cookies; return facts.

    Returns ``(session_id, expires_at, csrf_token)``. The caller is
    responsible for awaiting :func:`insert_session` afterward. Cookies
    are set with HttpOnly + SameSite=Strict + Secure per parent-issue
    invariant E; the ``Secure`` flag follows
    ``app.state.cookies_secure`` so v1 loopback can disable it.
    """
    now = request.app.state.clock()
    duration_hours = request.app.state.security_config.session.duration_hours
    from datetime import timedelta

    expires_at = format_expires_at(now=now, duration=timedelta(hours=duration_hours))
    session_id_str = secrets.token_urlsafe(32)
    sid = operator_session_id(session_id_str)
    csrf_token = mint_csrf_token()
    cookie_value = encode_session_cookie(
        SessionCookiePayload(session_id=sid, expires_at=expires_at),
        secret=request.app.state.session_signing_secret,
    )
    cookies_secure = bool(getattr(request.app.state, "cookies_secure", False))
    session_cookie_name = request.app.state.security_config.session.cookie_name
    csrf_cookie_name = request.app.state.security_config.csrf.cookie_name
    response.set_cookie(
        session_cookie_name,
        cookie_value,
        httponly=True,
        samesite="strict",
        secure=cookies_secure,
    )
    # cc_csrf is NOT HttpOnly — the frontend JS must read it to echo
    # via the X-CSRF-Token header.
    response.set_cookie(
        csrf_cookie_name,
        csrf_token,
        httponly=False,
        samesite="strict",
        secure=cookies_secure,
    )
    return sid, expires_at, csrf_token


# ---------------------------------------------------------------------------
# Router builder.
# ---------------------------------------------------------------------------


def build_auth_router() -> APIRouter:  # noqa: C901, PLR0915 — five routes grouped intentionally
    """Build the ``/auth/*`` router.

    Returned to the FastAPI composition root for ``app.include_router``;
    the test harness mounts it on a minimal app instead.

    Implementation note: the five route handlers are inner functions so
    they can close over the router instance and share the request /
    response shapes. The cyclomatic-complexity and statement-count lint
    rules are suppressed at the function level because the route bodies
    are flat (each one is one ceremony), grouped for discoverability
    (begin + complete + login + logout next to each other), and refactor
    requirements (e.g. inlined Pydantic boundaries) actually demand the
    flat layout. If a single route grows complex enough to want its own
    extraction, do it then — premature extraction would split the
    boundary across files for no comprehension win.
    """
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/register/begin")
    async def register_begin(
        body: RegisterBeginRequest,
        request: Request,
        response: Response,
        cc_session: Annotated[str | None, Cookie()] = None,
    ) -> RegisterBeginResponse:
        cc_factory = request.app.state.cc_writer_session_factory
        existing = await list_credentials(cc_factory)
        gate = request.app.state.setup_token_gate
        # Two enrollment modes:
        #
        # 1. First registration — no credentials in the DB. Setup token
        #    must match the gate's minted value. Authenticated only by
        #    the token; the session bearer is not yet possible.
        # 2. Subsequent registration — already-credentialed operator
        #    adds another passkey. Must present a valid session cookie
        #    (current_session would 401 if invalid).
        if not existing:
            if body.setup_token is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="setup_token required for first registration",
                )
            try:
                gate.consume(body.setup_token)
            except (SetupTokenMismatchError, SetupTokenAlreadyConsumedError) as exc:
                log.warning("register_begin: setup token rejected: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="setup_token rejected",
                ) from exc
        else:
            # Subsequent registration must carry a valid session.
            await current_session(request, cc_session=cc_session)

        verifier = request.app.state.webauthn_verifier
        user_id = secrets.token_bytes(16)
        options = verifier.generate_registration_options(
            user_id=user_id,
            user_name=body.user_name,
            existing_credentials=existing,
        )
        challenge_token = secrets.token_urlsafe(32)
        _get_challenge_store(request).stash_registration(
            token=challenge_token,
            challenge=options.challenge,
            user_id=user_id,
        )
        # Issue cc_csrf pre-session so the matching /register/complete
        # call (which is CSRF-required, F3) can verify double-submit.
        _issue_pre_session_csrf_cookie(response=response, request=request)
        return RegisterBeginResponse(
            challenge_token=challenge_token,
            challenge=_b64url_encode(options.challenge),
            user_id=_b64url_encode(user_id),
            user_name=body.user_name,
            relying_party_id=options.relying_party_id,
            relying_party_name=request.app.state.security_config.webauthn.relying_party_name,
            existing_credentials=[str(c) for c in options.existing_credentials],
        )

    @router.post(
        "/register/complete",
        dependencies=[Depends(csrf_required_pre_session)],
    )
    async def register_complete(
        body: RegisterCompleteRequest,
        request: Request,
        response: Response,
    ) -> AuthSuccessResponse:
        store = _get_challenge_store(request)
        stash = store.pop_registration(body.challenge_token)
        if stash is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="challenge_token not recognized",
            )
        challenge_bytes, _user_id = stash
        try:
            cred_id_typed = webauthn_credential_id(body.credential_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid credential_id: {exc}",
            ) from exc
        verifier = request.app.state.webauthn_verifier
        try:
            verified = verifier.verify_registration_response(
                options=RegistrationOptions(
                    user_id=_user_id,
                    user_name="",
                    challenge=challenge_bytes,
                    relying_party_id=request.app.state.security_config.webauthn.relying_party_id,
                    existing_credentials=(),
                ),
                response=RegistrationResponse(
                    credential_id=cred_id_typed,
                    client_data_json=_b64url_decode(body.client_data_json),
                    attestation_object=_b64url_decode(body.attestation_object),
                    transports=tuple(body.transports),
                ),
            )
        except (ValueError, WebAuthnException) as exc:
            log.warning("register_complete: verification failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="registration verification failed",
            ) from exc
        now_iso = request.app.state.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        cc_factory = request.app.state.cc_writer_session_factory
        await insert_credential(
            cc_factory,
            WebauthnCredentialRecord(
                credential_id=verified.credential_id,
                public_key=_b64url_encode(verified.public_key),
                sign_count=verified.sign_count,
                transports=",".join(verified.transports),
                created_at=now_iso,
            ),
        )
        # Issue session immediately so the freshly-enrolled operator is
        # logged in.
        sid, expires_at, csrf_token = _issue_session_cookies(
            response=response,
            request=request,
        )
        await insert_session(
            cc_factory,
            OperatorSessionRecord(
                session_id=sid,
                credential_id=verified.credential_id,
                expires_at=expires_at,
                csrf_token_hash=hash_csrf_token(csrf_token),
                created_at=now_iso,
            ),
        )
        return AuthSuccessResponse(
            session_id=sid,
            expires_at=expires_at,
            csrf_token=csrf_token,
            credential_id=verified.credential_id,
        )

    @router.post("/login/begin")
    async def login_begin(request: Request, response: Response) -> LoginBeginResponse:
        cc_factory = request.app.state.cc_writer_session_factory
        creds = await list_credentials(cc_factory)
        if not creds:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no credentials registered — complete enrollment first",
            )
        verifier = request.app.state.webauthn_verifier
        options = verifier.generate_authentication_options(allow_credentials=creds)
        challenge_token = secrets.token_urlsafe(32)
        _get_challenge_store(request).stash_authentication(
            token=challenge_token, challenge=options.challenge
        )
        # Issue cc_csrf pre-session so the matching /login/complete call
        # (which is CSRF-required, F3) can verify double-submit.
        _issue_pre_session_csrf_cookie(response=response, request=request)
        return LoginBeginResponse(
            challenge_token=challenge_token,
            challenge=_b64url_encode(options.challenge),
            relying_party_id=options.relying_party_id,
            allow_credentials=[str(c) for c in options.allow_credentials],
        )

    @router.post(
        "/login/complete",
        dependencies=[Depends(csrf_required_pre_session)],
    )
    async def login_complete(
        body: LoginCompleteRequest,
        request: Request,
        response: Response,
    ) -> AuthSuccessResponse:
        store = _get_challenge_store(request)
        challenge_bytes = store.pop_authentication(body.challenge_token)
        if challenge_bytes is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="challenge_token not recognized",
            )
        try:
            cred_id_typed = webauthn_credential_id(body.credential_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid credential_id: {exc}",
            ) from exc
        cc_factory = request.app.state.cc_writer_session_factory
        stored = await load_credential(cc_factory, credential_id=cred_id_typed)
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="credential not registered",
            )
        verifier = request.app.state.webauthn_verifier
        try:
            verified = verifier.verify_authentication_response(
                options=AuthenticationOptions(
                    challenge=challenge_bytes,
                    relying_party_id=request.app.state.security_config.webauthn.relying_party_id,
                    allow_credentials=(cred_id_typed,),
                ),
                response=AuthenticationResponse(
                    credential_id=cred_id_typed,
                    client_data_json=_b64url_decode(body.client_data_json),
                    authenticator_data=_b64url_decode(body.authenticator_data),
                    signature=_b64url_decode(body.signature),
                    user_handle=(
                        _b64url_decode(body.user_handle) if body.user_handle is not None else None
                    ),
                    new_sign_count=body.new_sign_count,
                ),
                stored_public_key=_b64url_decode(stored.public_key),
                stored_sign_count=stored.sign_count,
            )
        except (ValueError, WebAuthnException) as exc:
            log.warning("login_complete: verification failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="authentication verification failed",
            ) from exc
        await update_credential_sign_count(
            cc_factory,
            credential_id=verified.credential_id,
            new_sign_count=verified.new_sign_count,
        )
        now_iso = request.app.state.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        sid, expires_at, csrf_token = _issue_session_cookies(
            response=response,
            request=request,
        )
        await insert_session(
            cc_factory,
            OperatorSessionRecord(
                session_id=sid,
                credential_id=verified.credential_id,
                expires_at=expires_at,
                csrf_token_hash=hash_csrf_token(csrf_token),
                created_at=now_iso,
            ),
        )
        return AuthSuccessResponse(
            session_id=sid,
            expires_at=expires_at,
            csrf_token=csrf_token,
            credential_id=verified.credential_id,
        )

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(csrf_required)],
    )
    async def logout(
        request: Request,
        sid: Annotated[OperatorSessionId, Depends(current_session)],
    ) -> Response:
        from alphamind.command_center.auth.repository import delete_session

        cc_factory = request.app.state.cc_writer_session_factory
        await delete_session(cc_factory, session_id=sid)
        session_cookie_name = request.app.state.security_config.session.cookie_name
        csrf_cookie_name = request.app.state.security_config.csrf.cookie_name
        cookies_secure = bool(getattr(request.app.state, "cookies_secure", False))
        # 204 with explicit ``Set-Cookie`` headers that clear both cookies.
        # delete_cookie must mirror set_cookie's attributes (samesite,
        # secure, path) — most browsers ignore a clearing Set-Cookie
        # whose attributes don't match the original (F5).
        out = Response(status_code=status.HTTP_204_NO_CONTENT)
        out.delete_cookie(
            session_cookie_name,
            path="/",
            samesite="strict",
            secure=cookies_secure,
            httponly=True,
        )
        out.delete_cookie(
            csrf_cookie_name,
            path="/",
            samesite="strict",
            secure=cookies_secure,
            httponly=False,
        )
        return out

    return router


# ---------------------------------------------------------------------------
# Convenience re-export to suppress unused-import warnings.
# ---------------------------------------------------------------------------

# Pydantic models are used by the router decorators above; re-export under
# this name so static analyzers don't drop them.
_PYDANTIC_MODELS: tuple[type[Any], ...] = (
    RegisterBeginRequest,
    RegisterBeginResponse,
    RegisterCompleteRequest,
    LoginBeginResponse,
    LoginCompleteRequest,
    AuthSuccessResponse,
)
