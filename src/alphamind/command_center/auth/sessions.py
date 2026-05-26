"""Session-cookie + CSRF primitives (story 03 / ALP-667).

Pure functions over plain values (P1: functional core). The route layer
in :mod:`alphamind.command_center.auth.routes` is the imperative shell
that consumes these primitives.

Three concerns:

* **Session cookies.** :func:`encode_session_cookie` /
  :func:`decode_session_cookie` carry the :class:`SessionCookiePayload`
  (session id + expiry timestamp) signed by HMAC-SHA-256 with a process-
  startup secret. Tamper-evidence is structural: a flipped bit anywhere
  in the body causes signature mismatch on decode. The signing scheme
  uses stdlib ``hmac`` so the package picks up no extra dependency
  (``itsdangerous`` is not in our dependency tree).
* **CSRF tokens.** :func:`mint_csrf_token` issues a URL-safe random
  token via :func:`secrets.token_urlsafe`; :func:`hash_csrf_token`
  returns the SHA-256 hex digest the DB row carries. The cookie carries
  the raw token; the DB carries only the hash so a DB compromise yields
  hashes (which the route's verify step rejects unless the header
  echoes the cookie back). :func:`verify_csrf_token` does a constant-
  time hash comparison.
* **Session expiry.** :func:`is_session_expired` compares a stored
  expires-at ISO string against a Clock; :func:`format_expires_at`
  derives a fresh expires-at from a duration + Clock so the route layer
  doesn't grow its own datetime-format helper.

Per parent-issue invariant: cookies must be set ``HttpOnly``,
``SameSite=Strict``, ``Secure`` (LAN-only v1; ``Secure`` is configurable
because the design notes v1 is loopback-only). The route layer in
``routes.py`` owns the cookie-attribute discipline; this module produces
the encoded value.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from alphamind.command_center._kernel.ids import (
    OperatorSessionId,
    operator_session_id,
)

__all__ = [
    "InvalidSessionCookieError",
    "SessionCookiePayload",
    "decode_session_cookie",
    "encode_session_cookie",
    "format_expires_at",
    "hash_csrf_token",
    "is_session_expired",
    "mint_csrf_token",
    "verify_csrf_token",
]


_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# Session cookies.
# ---------------------------------------------------------------------------


class InvalidSessionCookieError(Exception):
    """Raised when :func:`decode_session_cookie` rejects a presented cookie.

    Reasons: malformed shape (no separator / non-base64url segments),
    signature mismatch (wrong secret or tampered body), or a body that
    doesn't decode to the documented :class:`SessionCookiePayload` shape.

    The route layer catches this and returns ``401 Unauthorized`` —
    the operator's response is identical to "no cookie presented" so
    a probing client gets no information about why the cookie was
    rejected.
    """


@dataclass(frozen=True, slots=True)
class SessionCookiePayload:
    """The decoded contents of the ``cc_session`` cookie.

    ``session_id`` is the FK target on ``operator_sessions``; the row
    is the authoritative state, the cookie is just a presented bearer.
    ``expires_at`` is the ISO-8601 timestamp at which the session
    becomes invalid; the route layer's :func:`current_session` rejects
    the cookie if the present time is past it.
    """

    session_id: OperatorSessionId
    expires_at: str


def encode_session_cookie(payload: SessionCookiePayload, *, secret: bytes) -> str:
    """Encode a session payload as a signed cookie value.

    Returns ``<base64url(body)>.<base64url(sig)>`` where ``body`` is the
    JSON-encoded payload bytes and ``sig`` is HMAC-SHA-256 over those
    raw bytes (NOT over the base64url-encoded form — F15). The two-
    segment format makes the cookie split trivially decodable; the HMAC
    over the raw JSON keeps the signature scheme canonical even if the
    base64url alphabet ever changes.
    """
    body = json.dumps(
        {"session_id": payload.session_id, "expires_at": payload.expires_at},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    body_b64 = _b64url_encode(body)
    sig_b64 = _b64url_encode(sig)
    return f"{body_b64}.{sig_b64}"


def decode_session_cookie(cookie: str, *, secret: bytes) -> SessionCookiePayload:
    """Decode a signed cookie value back to a :class:`SessionCookiePayload`.

    Raises :class:`InvalidSessionCookieError` on any rejection condition:
    missing separator, base64url decode failure, signature mismatch,
    JSON decode failure, missing payload fields.

    Constant-time signature comparison via :func:`hmac.compare_digest`
    so timing-attack measurements don't yield the signature.

    The HMAC is verified against the raw JSON bytes (F15), not the
    base64url-encoded form — matches :func:`encode_session_cookie`.
    """
    if not cookie or "." not in cookie:
        raise InvalidSessionCookieError("malformed cookie — missing separator")
    body_b64, sig_b64 = cookie.split(".", maxsplit=1)
    try:
        body = _b64url_decode(body_b64)
        presented_sig = _b64url_decode(sig_b64)
    except ValueError as exc:
        raise InvalidSessionCookieError("malformed cookie — invalid base64url") from exc
    expected_sig = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(presented_sig, expected_sig):
        raise InvalidSessionCookieError("signature mismatch")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidSessionCookieError("malformed cookie — invalid body JSON") from exc
    if not isinstance(data, dict):
        raise InvalidSessionCookieError("malformed cookie — body is not a JSON object")
    sid = data.get("session_id")
    exp = data.get("expires_at")
    if not isinstance(sid, str) or not isinstance(exp, str):
        raise InvalidSessionCookieError("malformed cookie — body missing session_id / expires_at")
    try:
        return SessionCookiePayload(
            session_id=operator_session_id(sid),
            expires_at=exp,
        )
    except ValueError as exc:
        raise InvalidSessionCookieError("malformed cookie — invalid session_id") from exc


# ---------------------------------------------------------------------------
# CSRF tokens (double-submit cookie pattern).
# ---------------------------------------------------------------------------


def mint_csrf_token() -> str:
    """Mint a fresh URL-safe CSRF token.

    The route layer sets this as the value of the ``cc_csrf`` cookie
    (NOT HttpOnly — the frontend JS must read it to echo back via the
    ``X-CSRF-Token`` header on state-mutating requests). The DB stores
    only :func:`hash_csrf_token` of the value.
    """
    return secrets.token_urlsafe(32)


def hash_csrf_token(token: str) -> str:
    """Hash a CSRF token for at-rest storage.

    SHA-256 hex digest. Stored in ``operator_sessions.csrf_token_hash``
    so a DB compromise yields hashes (which :func:`verify_csrf_token`
    rejects unless the presenter echoes the matching raw token).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_csrf_token(token: str, *, stored_hash: str) -> bool:
    """Return ``True`` iff *token* hashes to *stored_hash*.

    Constant-time hash comparison via :func:`hmac.compare_digest` so a
    timing-attack measurement of an empty-vs-present token doesn't
    leak information.
    """
    if not token:
        return False
    presented = hash_csrf_token(token)
    return hmac.compare_digest(presented, stored_hash)


# ---------------------------------------------------------------------------
# Session expiration.
# ---------------------------------------------------------------------------


def is_session_expired(expires_at: str, *, now: datetime) -> bool:
    """Return ``True`` iff *now* is at or past *expires_at*.

    Inclusive-at-expiry policy: at the moment of expiration the session
    is over. Mirrors browser ``Set-Cookie; Max-Age=N`` semantics —
    the cookie is gone exactly at the boundary.
    """
    boundary = datetime.strptime(expires_at, _ISO_FORMAT).replace(tzinfo=UTC)
    return now >= boundary


def format_expires_at(*, now: datetime, duration: timedelta) -> str:
    """Format a fresh expires-at ISO-8601 string.

    ``now + duration`` rendered as ``YYYY-MM-DDTHH:MM:SSZ`` matching the
    rest of the state-persistence layer's terminal-Z convention.
    """
    return (now + duration).astimezone(UTC).strftime(_ISO_FORMAT)


# ---------------------------------------------------------------------------
# Base64URL helpers (no padding — matches WebAuthn credential ID alphabet).
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    """Encode *data* as base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Decode base64url *text* (no padding) back to bytes.

    Raises :class:`ValueError` if the input is not valid base64url.
    """
    # base64 module requires padding; restore it before decoding.
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)
