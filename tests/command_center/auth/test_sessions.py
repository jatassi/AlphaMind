"""Tests for ``command_center.auth.sessions`` (story 03 / ALP-667).

Covers the pure session-cookie + CSRF primitives:

* :func:`encode_session_cookie` / :func:`decode_session_cookie` —
  HMAC-signed cookie carrying the session id + expiry timestamp.
* :func:`mint_csrf_token` / :func:`hash_csrf_token` —
  CSRF double-submit token + its SHA-256 hash for at-rest storage.
* :func:`is_session_expired` — pure expiry check against a Clock.

All four are pure functions over plain values (P1: functional core);
the route layer is the imperative shell that calls them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.auth.sessions import (
    InvalidSessionCookieError,
    SessionCookiePayload,
    decode_session_cookie,
    encode_session_cookie,
    hash_csrf_token,
    is_session_expired,
    mint_csrf_token,
    verify_csrf_token,
)

_SECRET = b"test-secret-key-32-bytes-long!!!!"


class TestEncodeSessionCookie:
    def test_round_trips_through_decode(self) -> None:
        payload = SessionCookiePayload(
            session_id=operator_session_id("sess-abc-123"),
            expires_at="2026-05-26T12:00:00Z",
        )
        cookie = encode_session_cookie(payload, secret=_SECRET)
        # Cookie is opaque text — base64url body + dot + base64url signature.
        decoded = decode_session_cookie(cookie, secret=_SECRET)
        assert decoded == payload

    def test_signature_uses_separator_and_two_segments(self) -> None:
        # The cookie format is documented as ``base64url(body).base64url(sig)``;
        # downstream tests use this fact to construct tampered cookies.
        payload = SessionCookiePayload(
            session_id=operator_session_id("sess"),
            expires_at="2026-05-26T12:00:00Z",
        )
        cookie = encode_session_cookie(payload, secret=_SECRET)
        assert cookie.count(".") == 1
        body, sig = cookie.split(".")
        assert body
        assert sig

    def test_hmac_is_over_raw_json_bytes_not_base64url(self) -> None:
        # F15: HMAC must be computed over the raw JSON bytes (the result
        # of json.dumps(...).encode("utf-8")) — not over the base64url-
        # encoded form. Verify by recomputing the HMAC manually against
        # the raw JSON and asserting it matches the signature in the
        # cookie.
        import base64
        import hashlib
        import hmac
        import json

        payload = SessionCookiePayload(
            session_id=operator_session_id("sess-hmac"),
            expires_at="2026-05-26T12:00:00Z",
        )
        cookie = encode_session_cookie(payload, secret=_SECRET)
        body_b64, sig_b64 = cookie.split(".")

        def _pad(s: str) -> str:
            return "=" * (-len(s) % 4)

        body_raw = base64.urlsafe_b64decode(body_b64 + _pad(body_b64))
        sig_raw = base64.urlsafe_b64decode(sig_b64 + _pad(sig_b64))
        # The HMAC matches HMAC(raw JSON bytes), NOT HMAC(base64url(body)).
        expected_over_raw = hmac.new(_SECRET, body_raw, hashlib.sha256).digest()
        expected_over_b64 = hmac.new(_SECRET, body_b64.encode("ascii"), hashlib.sha256).digest()
        assert sig_raw == expected_over_raw
        assert sig_raw != expected_over_b64
        # Sanity: body parses back to the same JSON payload.
        assert json.loads(body_raw.decode("utf-8")) == {
            "session_id": "sess-hmac",
            "expires_at": "2026-05-26T12:00:00Z",
        }


class TestDecodeSessionCookieRejection:
    def test_rejects_tampered_body(self) -> None:
        payload = SessionCookiePayload(
            session_id=operator_session_id("sess"),
            expires_at="2026-05-26T12:00:00Z",
        )
        cookie = encode_session_cookie(payload, secret=_SECRET)
        body, sig = cookie.split(".")
        # Flip a character in the body; signature now no longer matches.
        tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + sig
        with pytest.raises(InvalidSessionCookieError):
            decode_session_cookie(tampered, secret=_SECRET)

    def test_rejects_wrong_secret(self) -> None:
        payload = SessionCookiePayload(
            session_id=operator_session_id("sess"),
            expires_at="2026-05-26T12:00:00Z",
        )
        cookie = encode_session_cookie(payload, secret=_SECRET)
        with pytest.raises(InvalidSessionCookieError):
            decode_session_cookie(cookie, secret=b"different-secret-32-bytes-long!!")

    def test_rejects_malformed_cookie(self) -> None:
        with pytest.raises(InvalidSessionCookieError):
            decode_session_cookie("not-a-cookie", secret=_SECRET)

    def test_rejects_empty_cookie(self) -> None:
        with pytest.raises(InvalidSessionCookieError):
            decode_session_cookie("", secret=_SECRET)


class TestIsSessionExpired:
    def test_returns_false_when_now_is_before_expires_at(self) -> None:
        now = datetime(2026, 5, 26, 11, 0, 0, tzinfo=UTC)
        # expires_at is one hour after now.
        assert not is_session_expired("2026-05-26T12:00:00Z", now=now)

    def test_returns_true_when_now_is_after_expires_at(self) -> None:
        now = datetime(2026, 5, 26, 13, 0, 0, tzinfo=UTC)
        # expires_at is one hour before now.
        assert is_session_expired("2026-05-26T12:00:00Z", now=now)

    def test_returns_true_at_exact_expiry(self) -> None:
        # Inclusive-at-expiry policy — at the moment of expiration the
        # session is over. Mirrors how most ``Set-Cookie; Max-Age`` is
        # interpreted by browsers: the cookie is gone at expiry.
        now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        assert is_session_expired("2026-05-26T12:00:00Z", now=now)


class TestCsrfToken:
    def test_mint_returns_non_empty_url_safe_token(self) -> None:
        token = mint_csrf_token()
        assert token
        # token must be safe to ship as a cookie value: alphanumeric + - _.
        assert all(c.isalnum() or c in "-_" for c in token)

    def test_hash_is_stable_for_same_token(self) -> None:
        token = mint_csrf_token()
        assert hash_csrf_token(token) == hash_csrf_token(token)

    def test_hash_differs_for_different_tokens(self) -> None:
        assert hash_csrf_token("a") != hash_csrf_token("b")

    def test_each_mint_returns_a_fresh_token(self) -> None:
        assert mint_csrf_token() != mint_csrf_token()


class TestVerifyCsrfToken:
    def test_returns_true_when_token_matches_stored_hash(self) -> None:
        token = mint_csrf_token()
        stored = hash_csrf_token(token)
        assert verify_csrf_token(token, stored_hash=stored)

    def test_returns_false_when_token_does_not_match(self) -> None:
        stored = hash_csrf_token("the-real-token")
        assert not verify_csrf_token("a-different-token", stored_hash=stored)

    def test_returns_false_when_token_is_empty(self) -> None:
        stored = hash_csrf_token("the-real-token")
        assert not verify_csrf_token("", stored_hash=stored)


class TestSessionExpirationHelper:
    def test_format_expires_at_returns_isoformat_with_trailing_z(self) -> None:
        # Helper that lets the route layer derive expires_at from a
        # SessionConfig duration_hours value + a Clock. Kept here (as a
        # session primitive) so the route doesn't grow a datetime-format
        # helper of its own.
        from alphamind.command_center.auth.sessions import format_expires_at

        now = datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC)
        expires_at = format_expires_at(now=now, duration=timedelta(hours=12))
        assert expires_at == "2026-05-26T12:00:00Z"
