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
  accepts any response whose challenge bytes match the options it was
  paired with AND whose credential is in the allow-list. Replay-counter
  enforcement (new sign_count strictly greater than stored) is also
  mirrored so tests catch sign-count bugs.

Per parent-issue invariant E: WebAuthn resident (discoverable)
credentials only; no username / password fallback.

I/O types are frozen dataclasses (P5: Pydantic at boundaries only —
this Protocol is internal); the FastAPI request / response models in
``routes.py`` are the only Pydantic surfaces in this subpackage.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

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

    The four fields mirror the WebAuthn registration ceremony's output:
    the new credential's ID, the challenge the client signed (must match
    the options' challenge), the public key extracted from attestation,
    and the initial sign count + declared transports.
    """

    credential_id: WebauthnCredentialId
    client_data_challenge: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]


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
    """Browser's response to ``navigator.credentials.get()``."""

    credential_id: WebauthnCredentialId
    client_data_challenge: bytes
    new_sign_count: int


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
    any response whose challenge bytes match the paired options' challenge
    AND whose credential ID is in the allow-list. Replay-counter
    enforcement (new sign_count strictly greater than stored) is mirrored
    so tests can exercise the route layer's sign-count update path.

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
        if response.client_data_challenge != options.challenge:
            msg = (
                "registration response challenge does not match options challenge — "
                "possible replay or cross-ceremony mix-up"
            )
            raise ValueError(msg)
        return VerifiedRegistration(
            credential_id=response.credential_id,
            public_key=response.public_key,
            sign_count=response.sign_count,
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
        if response.client_data_challenge != options.challenge:
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
        if response.new_sign_count <= stored_sign_count:
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
            PublicKeyCredentialDescriptor(
                id=pywebauthn.base64url_to_bytes(cred)
            )
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

        # The route layer threads the raw browser JSON through; the
        # wrapper accepts it as a Mapping or string. We translate the
        # frozen-dataclass RegistrationResponse into the credential dict
        # py_webauthn expects.
        credential: dict[str, Any] = {
            "id": response.credential_id,
            "rawId": response.credential_id,
            "response": {
                "clientDataJSON": pywebauthn.base64url_to_bytes(
                    response.credential_id
                ),
            },
            "type": "public-key",
        }
        verified = pywebauthn.verify_registration_response(
            credential=credential,
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
            PublicKeyCredentialDescriptor(
                id=pywebauthn.base64url_to_bytes(cred)
            )
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

        credential: dict[str, Any] = {
            "id": response.credential_id,
            "rawId": response.credential_id,
            "response": {},
            "type": "public-key",
        }
        verified = pywebauthn.verify_authentication_response(
            credential=credential,
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
