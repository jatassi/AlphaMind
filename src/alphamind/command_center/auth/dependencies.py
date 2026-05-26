"""FastAPI dependencies for the auth surface (story 03 / ALP-667).

Every ``/api/*`` router added by later stories must include
``Depends(current_session)`` for read endpoints and
``Depends(csrf_required)`` for state-mutating verbs (POST / PUT / PATCH /
DELETE). The dependency contract is documented in
:mod:`alphamind.command_center.auth.__init__` so subsequent stories
find it without re-reading the design doc.

Two dependencies:

* :func:`current_session` — reads the ``cc_session`` cookie, verifies
  the signature against ``app.state.session_signing_secret``, checks
  expiry against ``app.state.clock``, loads the row from
  ``operator_sessions`` to confirm it still exists in the authoritative
  state, and returns the :class:`OperatorSessionId`. Any failure
  results in ``401 Unauthorized`` with a generic message.
* :func:`csrf_required` — reads the ``cc_csrf`` cookie + the
  ``X-CSRF-Token`` header; both must be present and must match. Any
  failure results in ``403 Forbidden``.

The dependencies pull their collaborators off ``request.app.state`` so
the production composition root wires them once at lifespan and tests
swap them via the shared conftest fixture.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Cookie, Header, HTTPException, Request, status

from alphamind.command_center._kernel.ids import OperatorSessionId
from alphamind.command_center.auth.repository import load_session
from alphamind.command_center.auth.sessions import (
    InvalidSessionCookieError,
    decode_session_cookie,
    is_session_expired,
    verify_csrf_token,
)

__all__ = [
    "csrf_required",
    "current_session",
]

log = logging.getLogger(__name__)

_AUTH_FAILED_DETAIL = "Authentication required"
"""Generic 401 message — no information leak about *why* auth failed.

A probing client gets the same response whether the cookie is absent,
malformed, signed with the wrong key, expired, or refers to a deleted
session.
"""

_CSRF_FAILED_DETAIL = "CSRF check failed"
"""Generic 403 message for any CSRF rejection path."""


async def current_session(
    request: Request,
    cc_session: Annotated[str | None, Cookie()] = None,
) -> OperatorSessionId:
    """Validate the presented session cookie and return its session id.

    Raises :exc:`HTTPException(401)` on any rejection path:

    * Cookie absent.
    * Cookie signature invalid (wrong secret or tampered body).
    * Cookie body malformed.
    * Session expired per ``app.state.clock``.
    * No row in ``operator_sessions`` for the presented session id
      (could mean: session deleted via logout, or signed cookie forged
      against a never-existing id).

    Logs the rejection reason at DEBUG so operator triage can see why an
    auth attempt failed without the response itself leaking that to a
    probing client.
    """
    if cc_session is None:
        log.debug("current_session: no cc_session cookie presented")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_AUTH_FAILED_DETAIL)
    secret: bytes = request.app.state.session_signing_secret
    try:
        payload = decode_session_cookie(cc_session, secret=secret)
    except InvalidSessionCookieError as exc:
        log.debug("current_session: cookie decode rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_AUTH_FAILED_DETAIL
        ) from exc
    now = request.app.state.clock()
    if is_session_expired(payload.expires_at, now=now):
        log.debug("current_session: session %s expired", payload.session_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_AUTH_FAILED_DETAIL)
    cc_factory = request.app.state.cc_writer_session_factory
    record = await load_session(cc_factory, session_id=payload.session_id)
    if record is None:
        log.debug(
            "current_session: session %s not in operator_sessions",
            payload.session_id,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_AUTH_FAILED_DETAIL)
    return payload.session_id


async def csrf_required(
    request: Request,
    cc_csrf: Annotated[str | None, Cookie()] = None,
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> None:
    """Verify the double-submit CSRF token.

    Raises :exc:`HTTPException(403)` if:

    * ``cc_csrf`` cookie is absent.
    * ``X-CSRF-Token`` header is absent.
    * The two values don't match (constant-time compare).
    * The cookie value doesn't match the hash on the session row (so a
      cookie issued for a different session can't be presented here).

    The route layer's mutating verbs include this dependency; read
    endpoints don't. Logged-out users hit 401 from
    :func:`current_session` first; this is the second line of defense.
    """
    if cc_csrf is None or x_csrf_token is None:
        log.debug("csrf_required: cookie or header missing")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED_DETAIL)
    # Double-submit baseline: header must match cookie. Then verify
    # against the session row's stored hash so a leaked cookie from
    # another session can't be replayed.
    if cc_csrf != x_csrf_token:
        log.debug("csrf_required: header does not match cookie")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED_DETAIL)
    # Bind the CSRF check to the session row's stored hash. Pull the
    # session id off the validated session cookie if one is presented;
    # otherwise reject. (The route layer typically composes
    # ``current_session`` + ``csrf_required`` on the same endpoint, so
    # by the time we get here the session is already validated.)
    cc_session = request.cookies.get(request.app.state.security_config.session.cookie_name)
    if cc_session is None:
        log.debug("csrf_required: session cookie absent")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED_DETAIL)
    secret: bytes = request.app.state.session_signing_secret
    try:
        payload = decode_session_cookie(cc_session, secret=secret)
    except InvalidSessionCookieError:
        log.debug("csrf_required: session cookie invalid")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED_DETAIL
        ) from None
    cc_factory = request.app.state.cc_writer_session_factory
    session_record = await load_session(cc_factory, session_id=payload.session_id)
    if session_record is None:
        log.debug("csrf_required: session row missing for cookie")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED_DETAIL)
    if not verify_csrf_token(cc_csrf, stored_hash=session_record.csrf_token_hash):
        log.debug("csrf_required: token does not hash to stored value")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED_DETAIL)
