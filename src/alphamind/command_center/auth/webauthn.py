"""WebAuthn relying-party surface (story 03 / ALP-667).

This module pins the four ``py_webauthn`` calls behind a Protocol so a
library upgrade doesn't ripple through the routes, and so unit tests
can swap in an in-memory fake (parent-issue invariant: "Protocols +
in-memory fakes").

Three shapes:

* :class:`WebauthnVerifier` — the Protocol the route layer consumes.
* :class:`RealWebauthnVerifier` — wraps ``py_webauthn``. Exercised end-
  to-end ONLY by the verify script (story 07).
* :class:`InMemoryWebauthnVerifier` — deterministic fake used by unit
  tests. Generates a fresh random challenge per call; the verify path
  accepts any response whose credential is in the allow-list. Replay-
  counter enforcement (per WebAuthn spec § 7.2: new sign_count strictly
  greater than stored, but ONLY when either is non-zero) is mirrored so
  tests catch sign-count bugs.

Per parent-issue invariant E: WebAuthn resident (discoverable)
credentials only; no username / password fallback.

I/O types are frozen dataclasses (P5: Pydantic at boundaries only —
this Protocol is internal); the FastAPI request / response models in
``routes.py`` are the only Pydantic surfaces in this subpackage.

The I/O types carry the raw browser blobs (clientDataJSON,
attestationObject, authenticatorData, signature) that ``py_webauthn``
needs for cryptographic verification. The browser-side WebAuthn JSON
emits these as base64url strings; the routes layer decodes at the
Pydantic boundary so this module only ever sees bytes.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from alphamind.command_center._kernel.ids import (
    WebauthnCredentialId,
    webauthn_credential_id,
)

__all__ = [
    "AuthenticationOptions",
    "AuthenticationResponse",
    "InMemoryWebauthnVerifier",
    "RealWebauthnVerifier",
    "RegistrationOptions",
    "RegistrationResponse",
    "VerifiedAuthentication",
    "VerifiedRegistration",
    "WebauthnVerifier",
]


# ---------------------------------------------------------------------------
# I/O dataclasses (P5: Pydantic at boundaries only).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistrationOptions:
    """Options the browser passes to ``navigator.credentials.create()``.

    The ``challenge`` is the bytes the authenticator must echo back; the
    relying party verifies the echo matches what it sent. Per WebAuthn
    spec, challenges must be cryptographically random and never reused.
    """

    user_id: bytes
    user_name: str
    challenge: bytes
    relying_party_id: str
    existing_credentials: tuple[WebauthnCredentialId, ...]


@dataclass(frozen=True, slots=True)
class RegistrationResponse:
    """Browser's response to ``navigator.credentials.create()``.

    Fields mirror the raw blobs ``py_webauthn``'s
    ``verify_registration_response`` requires:

    * ``credential_id``: the new credential's relying-party ID.
    * ``client_data_json``: the browser's raw ``clientDataJSON`` blob
      (base64url-decoded). py_webauthn re-parses it to extract the echo
      of the challenge + the origin claim.
    * ``attestation_object``: the browser's raw ``attestationObject``
      blob (base64url-decoded). py_webauthn re-parses it to extract the
      public key + sign count + attestation statement.
    * ``transports``: optional list the authenticator declared (USB,
      NFC, BLE, internal). Stored alongside the credential for the
      login ceremony's transport hint.
    """

    credential_id: WebauthnCredentialId
    client_data_json: bytes
    attestation_object: bytes
    transports: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerifiedRegistration:
    """Verified registration ceremony result.

    The relying party persists the four fields here to
    ``webauthn_credentials`` (story 03 routes); the public key + sign
    count drive future assertion verification.
    """

    credential_id: WebauthnCredentialId
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthenticationOptions:
    """Options the browser passes to ``navigator.credentials.get()``."""

    challenge: bytes
    relying_party_id: str
    allow_credentials: tuple[WebauthnCredentialId, ...]


@dataclass(frozen=True, slots=True)
class AuthenticationResponse:
    """Browser's response to ``navigator.credentials.get()``.

    Fields mirror the raw blobs ``py_webauthn``'s
    ``verify_authentication_response`` requires:

    * ``credential_id``: the asserted credential's relying-party ID.
    * ``client_data_json``: the browser's raw ``clientDataJSON`` blob.
    * ``authenticator_data``: the raw ``authenticatorData`` blob — carries
      the sign count + UV/UP flags.
    * ``signature``: the assertion signature over
      ``authenticatorData || sha256(clientDataJSON)``.
    * ``user_handle``: optional user-handle blob (resident-credential
      discovery). The browser emits this when the assertion was via a
      discoverable credential.
    * ``new_sign_count``: the parsed counter from ``authenticatorData``.
      Passed alongside the raw bytes for convenience — the route layer
      persists this value after verification succeeds.
    """

    credential_id: WebauthnCredentialId
    client_data_json: bytes
    authenticator_data: bytes
    signature: bytes
    new_sign_count: int
    user_handle: bytes | None = None


@dataclass(frozen=True, slots=True)
class VerifiedAuthentication:
    """Verified authentication ceremony result.

    The relying party updates the credential's ``sign_count`` to
    ``new_sign_count`` (story 03 routes) — a sign-count drop or stall
    on a subsequent assertion indicates a cloned authenticator.
    """

    credential_id: WebauthnCredentialId
    new_sign_count: int


# ---------------------------------------------------------------------------
# Protocol.
# ---------------------------------------------------------------------------


class WebauthnVerifier(Protocol):
    """Relying-party surface the route layer consumes.

    Four methods mirror the four ``py_webauthn`` calls:
    ``generate_registration_options`` / ``verify_registration_response`` /
    ``generate_authentication_options`` / ``verify_authentication_response``.
    Implementations may be real (wraps ``py_webauthn``) or in-memory
    (deterministic fake for unit tests).
    """

    def generate_registration_options(
        self,
        *,
        user_id: bytes,
        user_name: str,
        existing_credentials: Sequence[WebauthnCredentialId],
    ) -> RegistrationOptions: ...

    def verify_registration_response(
        self,
        *,
        options: RegistrationOptions,
        response: RegistrationResponse,
    ) -> VerifiedRegistration: ...

    def generate_authentication_options(
        self,
        *,
        allow_credentials: Sequence[WebauthnCredentialId],
    ) -> AuthenticationOptions: ...

    def verify_authentication_response(
        self,
        *,
        options: AuthenticationOptions,
        response: AuthenticationResponse,
        stored_public_key: bytes,
        stored_sign_count: int,
    ) -> VerifiedAuthentication: ...


# ---------------------------------------------------------------------------
# InMemoryWebauthnVerifier — deterministic fake for unit tests.
# ---------------------------------------------------------------------------


class InMemoryWebauthnVerifier:
    """Deterministic fake used by unit tests.

    Generates a fresh 32-byte challenge per call; the verify path accepts
    any response whose embedded challenge (parsed out of the synthetic
    ``client_data_json`` blob) matches the paired options' challenge AND
    whose credential ID is in the allow-list. Replay-counter enforcement
    mirrors py_webauthn's semantics (per spec § 7.2 / py_webauthn:
    enforce strict-increase only when EITHER stored OR new sign_count is
    non-zero — counter-not-supported authenticators report both as 0 and
    that is acceptable).

    The fake's ``client_data_json`` shape is a small JSON blob carrying
    ``{"challenge": "<base64url>"}``; tests construct it via the
    :func:`encode_inmemory_client_data_json` helper. No actual crypto.

    No state is shared across instances; each test gets a fresh verifier.
    """

    def __init__(self, *, relying_party_id: str = "localhost") -> None:
        self._relying_party_id = relying_party_id

    def generate_registration_options(
        self,
        *,
        user_id: bytes,
        user_name: str,
        existing_credentials: Sequence[WebauthnCredentialId],
    ) -> RegistrationOptions:
        return RegistrationOptions(
            user_id=user_id,
            user_name=user_name,
            challenge=secrets.token_bytes(32),
            relying_party_id=self._relying_party_id,
            existing_credentials=tuple(existing_credentials),
        )

    def verify_registration_response(
        self,
        *,
        options: RegistrationOptions,
        response: RegistrationResponse,
    ) -> VerifiedRegistration:
        challenge_seen = _extract_inmemory_challenge(response.client_data_json)
        if challenge_seen != options.challenge:
            msg = (
                "registration response challenge does not match options challenge — "
                "possible replay or cross-ceremony mix-up"
            )
            raise ValueError(msg)
        # The synthetic attestation_object blob carries a JSON envelope
        # with the public-key bytes + initial sign count. Real ceremonies
        # parse a CBOR-encoded blob; the in-memory fake takes the shortcut.
        public_key, sign_count = _parse_inmemory_attestation_object(response.attestation_object)
        return VerifiedRegistration(
            credential_id=response.credential_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=response.transports,
        )

    def generate_authentication_options(
        self,
        *,
        allow_credentials: Sequence[WebauthnCredentialId],
    ) -> AuthenticationOptions:
        return AuthenticationOptions(
            challenge=secrets.token_bytes(32),
            relying_party_id=self._relying_party_id,
            allow_credentials=tuple(allow_credentials),
        )

    def verify_authentication_response(
        self,
        *,
        options: AuthenticationOptions,
        response: AuthenticationResponse,
        stored_public_key: bytes,
        stored_sign_count: int,
    ) -> VerifiedAuthentication:
        # ``stored_public_key`` is part of the Protocol surface because
        # :class:`RealWebauthnVerifier` needs it for signature
        # verification; the in-memory fake skips crypto entirely and
        # asserts only the structural invariants (challenge match,
        # credential in allow-list, sign-count increment).
        del stored_public_key
        challenge_seen = _extract_inmemory_challenge(response.client_data_json)
        if challenge_seen != options.challenge:
            msg = (
                "authentication response challenge does not match options challenge — "
                "possible replay or cross-ceremony mix-up"
            )
            raise ValueError(msg)
        if response.credential_id not in options.allow_credentials:
            msg = (
                f"credential {response.credential_id!r} not in allow_credentials — "
                "browser presented an unregistered passkey"
            )
            raise ValueError(msg)
        # py_webauthn semantics (verify_authentication_response.py § 149):
        # enforce strict-increase ONLY when either side is non-zero.
        # Authenticators that don't support counters report 0 forever; the
        # 0/0 case must NOT be rejected.
        if (response.new_sign_count > 0 or stored_sign_count > 0) and (
            response.new_sign_count <= stored_sign_count
        ):
            msg = (
                f"sign_count must strictly increase; got new={response.new_sign_count} "
                f"vs stored={stored_sign_count} — possible cloned authenticator"
            )
            raise ValueError(msg)
        return VerifiedAuthentication(
            credential_id=response.credential_id,
            new_sign_count=response.new_sign_count,
        )


# ---------------------------------------------------------------------------
# In-memory verifier helpers (test-only synthetic blob shapes).
# ---------------------------------------------------------------------------


def encode_inmemory_client_data_json(challenge: bytes) -> bytes:
    """Encode a synthetic ``clientDataJSON`` blob carrying *challenge*.

    Real browsers emit a JSON object whose ``challenge`` field is the
    base64url-encoded challenge; the in-memory fake mirrors that single
    field. Tests use this helper to build the
    :class:`RegistrationResponse` / :class:`AuthenticationResponse`
    inputs without re-implementing the encoding inline.
    """
    import base64
    import json

    encoded = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode("ascii")
    return json.dumps({"challenge": encoded}, separators=(",", ":")).encode("utf-8")


def encode_inmemory_attestation_object(*, public_key: bytes, sign_count: int) -> bytes:
    """Encode a synthetic ``attestationObject`` blob.

    Real ceremonies emit a CBOR-encoded blob; the in-memory fake uses a
    JSON envelope so tests don't need to depend on a CBOR library. The
    blob carries the public-key bytes (base64url-encoded) + the initial
    sign count so :meth:`InMemoryWebauthnVerifier.verify_registration_response`
    can recover them.
    """
    import base64
    import json

    pk_b64 = base64.urlsafe_b64encode(public_key).rstrip(b"=").decode("ascii")
    return json.dumps(
        {"public_key": pk_b64, "sign_count": sign_count},
        separators=(",", ":"),
    ).encode("utf-8")


def _extract_inmemory_challenge(client_data_json: bytes) -> bytes:
    """Parse the challenge bytes out of a synthetic ``clientDataJSON``.

    Raises ``ValueError`` on any structural failure — the route layer
    catches ``ValueError`` and maps to ``400 Bad Request``.
    """
    import base64
    import json

    try:
        data = json.loads(client_data_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "in-memory verifier: client_data_json is not valid JSON"
        raise ValueError(msg) from exc
    if not isinstance(data, dict) or "challenge" not in data:
        msg = "in-memory verifier: client_data_json missing 'challenge' field"
        raise ValueError(msg)
    encoded = data["challenge"]
    if not isinstance(encoded, str):
        msg = "in-memory verifier: client_data_json 'challenge' is not a string"
        raise ValueError(msg)  # noqa: TRY004 — domain error, not a type error
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def _parse_inmemory_attestation_object(attestation_object: bytes) -> tuple[bytes, int]:
    """Recover (public_key, sign_count) from a synthetic ``attestationObject``.

    Raises ``ValueError`` on any structural failure — symmetric with
    :func:`_extract_inmemory_challenge`.
    """
    import base64
    import json

    try:
        data = json.loads(attestation_object.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "in-memory verifier: attestation_object is not valid JSON"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = "in-memory verifier: attestation_object is not a JSON object"
        raise ValueError(msg)  # noqa: TRY004 — domain error, not a type error
    pk_encoded = data.get("public_key")
    sign_count = data.get("sign_count")
    if not isinstance(pk_encoded, str) or not isinstance(sign_count, int):
        msg = "in-memory verifier: attestation_object missing public_key / sign_count"
        raise ValueError(msg)  # noqa: TRY004 — domain error, not a type error
    padding = "=" * (-len(pk_encoded) % 4)
    public_key = base64.urlsafe_b64decode(pk_encoded + padding)
    return public_key, sign_count


# ---------------------------------------------------------------------------
# RealWebauthnVerifier — wraps py_webauthn.
# ---------------------------------------------------------------------------


class RealWebauthnVerifier:
    """Wraps the four ``py_webauthn`` calls.

    Exercised end-to-end ONLY by the verify script (story 07), against
    a real authenticator. Unit tests use :class:`InMemoryWebauthnVerifier`
    so the test surface never touches real ``py_webauthn`` parsing.

    The wrapping translates between this module's frozen-dataclass I/O
    types and ``py_webauthn``'s own dataclass shapes; that translation
    is the seam an upstream library upgrade can ripple through without
    touching the route layer or tests.

    Construction takes the relying-party id + name + origin, all sourced
    from :class:`alphamind.command_center.config.SecurityConfig`.
    """

    def __init__(
        self,
        *,
        relying_party_id: str,
        relying_party_name: str,
        expected_origin: str,
    ) -> None:
        self._relying_party_id = relying_party_id
        self._relying_party_name = relying_party_name
        self._expected_origin = expected_origin
        # Per-ceremony challenge bookkeeping — RealWebauthnVerifier keeps
        # the most recently generated registration / authentication
        # challenges so verify_*_response can match them. In production
        # the route layer threads challenges through session-bound state
        # so this isn't strictly necessary; the wrapper holds them as
        # a convenience for the script-level smoke flow.

    def generate_registration_options(
        self,
        *,
        user_id: bytes,
        user_name: str,
        existing_credentials: Sequence[WebauthnCredentialId],
    ) -> RegistrationOptions:
        # Import inside the method so unit tests never pull py_webauthn
        # into sys.modules when only the in-memory fake is exercised.
        import webauthn as pywebauthn
        from webauthn.helpers.structs import (
            PublicKeyCredentialDescriptor,
        )

        existing = [
            PublicKeyCredentialDescriptor(id=pywebauthn.base64url_to_bytes(cred))
            for cred in existing_credentials
        ]
        options = pywebauthn.generate_registration_options(
            rp_id=self._relying_party_id,
            rp_name=self._relying_party_name,
            user_name=user_name,
            user_id=user_id,
            exclude_credentials=existing,
        )
        return RegistrationOptions(
            user_id=user_id,
            user_name=user_name,
            challenge=bytes(options.challenge),
            relying_party_id=self._relying_party_id,
            existing_credentials=tuple(existing_credentials),
        )

    def verify_registration_response(
        self,
        *,
        options: RegistrationOptions,
        response: RegistrationResponse,
    ) -> VerifiedRegistration:
        import webauthn as pywebauthn

        # py_webauthn's ``verify_registration_response`` accepts a dict
        # mirroring ``navigator.credentials.create()``'s output: an
        # outer object with the credential ID + a ``response`` sub-object
        # carrying the raw ``clientDataJSON`` + ``attestationObject``
        # blobs. The route layer base64url-decodes those at the Pydantic
        # boundary so we ship raw bytes from here on.
        credential_dict: dict[str, object] = {
            "id": response.credential_id,
            "rawId": _b64url_encode(response.credential_id.encode("ascii")),
            "response": {
                "clientDataJSON": _b64url_encode(response.client_data_json),
                "attestationObject": _b64url_encode(response.attestation_object),
                "transports": list(response.transports),
            },
            "type": "public-key",
        }
        verified = pywebauthn.verify_registration_response(
            credential=credential_dict,
            expected_challenge=options.challenge,
            expected_rp_id=self._relying_party_id,
            expected_origin=self._expected_origin,
        )
        return VerifiedRegistration(
            credential_id=webauthn_credential_id(response.credential_id),
            public_key=bytes(verified.credential_public_key),
            sign_count=int(verified.sign_count),
            transports=response.transports,
        )

    def generate_authentication_options(
        self,
        *,
        allow_credentials: Sequence[WebauthnCredentialId],
    ) -> AuthenticationOptions:
        import webauthn as pywebauthn
        from webauthn.helpers.structs import PublicKeyCredentialDescriptor

        allow = [
            PublicKeyCredentialDescriptor(id=pywebauthn.base64url_to_bytes(cred))
            for cred in allow_credentials
        ]
        options = pywebauthn.generate_authentication_options(
            rp_id=self._relying_party_id,
            allow_credentials=allow,
        )
        return AuthenticationOptions(
            challenge=bytes(options.challenge),
            relying_party_id=self._relying_party_id,
            allow_credentials=tuple(allow_credentials),
        )

    def verify_authentication_response(
        self,
        *,
        options: AuthenticationOptions,
        response: AuthenticationResponse,
        stored_public_key: bytes,
        stored_sign_count: int,
    ) -> VerifiedAuthentication:
        import webauthn as pywebauthn

        # py_webauthn's ``verify_authentication_response`` accepts a dict
        # mirroring ``navigator.credentials.get()``'s output: ``response``
        # carries raw ``clientDataJSON`` + ``authenticatorData`` +
        # ``signature`` blobs, plus an optional ``userHandle`` for
        # resident credentials.
        response_subobj: dict[str, object] = {
            "clientDataJSON": _b64url_encode(response.client_data_json),
            "authenticatorData": _b64url_encode(response.authenticator_data),
            "signature": _b64url_encode(response.signature),
        }
        if response.user_handle is not None:
            response_subobj["userHandle"] = _b64url_encode(response.user_handle)
        credential_dict: dict[str, object] = {
            "id": response.credential_id,
            "rawId": _b64url_encode(response.credential_id.encode("ascii")),
            "response": response_subobj,
            "type": "public-key",
        }
        verified = pywebauthn.verify_authentication_response(
            credential=credential_dict,
            expected_challenge=options.challenge,
            expected_rp_id=self._relying_party_id,
            expected_origin=self._expected_origin,
            credential_public_key=stored_public_key,
            credential_current_sign_count=stored_sign_count,
        )
        return VerifiedAuthentication(
            credential_id=webauthn_credential_id(response.credential_id),
            new_sign_count=int(verified.new_sign_count),
        )


def _b64url_encode(data: bytes) -> str:
    """Encode *data* as base64url without padding (py_webauthn's input form)."""
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
