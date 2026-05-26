"""Tests for ``command_center.auth.webauthn`` (story 03 / ALP-667).

Covers the :class:`WebauthnVerifier` Protocol surface and its two
implementations:

* :class:`InMemoryWebauthnVerifier` — deterministic fake used by unit
  tests. Returns canned challenges and accepts any response whose
  ``credential_id`` matches.
* :class:`RealWebauthnVerifier` — wraps the four ``py_webauthn`` calls.
  Not exercised here; the verify script (story 07) exercises real
  ``py_webauthn`` against a real authenticator.

The Protocol's I/O types are frozen dataclasses (P5: Pydantic at
boundaries only); the FastAPI request / response models in
``routes.py`` are the only Pydantic surfaces in this story.
"""

from __future__ import annotations

import pytest

from alphamind.command_center._kernel.ids import webauthn_credential_id
from alphamind.command_center.auth.webauthn import (
    AuthenticationOptions,
    AuthenticationResponse,
    InMemoryWebauthnVerifier,
    RegistrationOptions,
    RegistrationResponse,
    VerifiedAuthentication,
    VerifiedRegistration,
    WebauthnVerifier,
)


class TestInMemoryWebauthnVerifierImplementsProtocol:
    def test_satisfies_webauthn_verifier_protocol(self) -> None:
        # Structural check — if the in-memory verifier missed any method
        # or got the signatures wrong, mypy would fail. The runtime check
        # confirms the Protocol's required attributes exist on the instance
        # so downstream consumers can rely on the duck-typed surface.
        verifier: WebauthnVerifier = InMemoryWebauthnVerifier()
        assert hasattr(verifier, "generate_registration_options")
        assert hasattr(verifier, "verify_registration_response")
        assert hasattr(verifier, "generate_authentication_options")
        assert hasattr(verifier, "verify_authentication_response")


class TestInMemoryRegistrationOptions:
    def test_returns_options_with_user_id_and_user_name(self) -> None:
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        options = verifier.generate_registration_options(
            user_id=b"operator",
            user_name="operator",
            existing_credentials=(),
        )
        assert isinstance(options, RegistrationOptions)
        assert options.user_id == b"operator"
        assert options.user_name == "operator"
        # A non-empty challenge is required so the verify path has
        # something to match.
        assert options.challenge != b""
        assert options.relying_party_id == "localhost"

    def test_each_call_returns_a_fresh_challenge(self) -> None:
        # Distinct registration ceremonies must not share a challenge —
        # the WebAuthn spec requires it. The in-memory fake mirrors this
        # so tests catch replay-style bugs.
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        a = verifier.generate_registration_options(
            user_id=b"u", user_name="u", existing_credentials=()
        )
        b = verifier.generate_registration_options(
            user_id=b"u", user_name="u", existing_credentials=()
        )
        assert a.challenge != b.challenge


class TestInMemoryVerifyRegistrationResponse:
    def test_returns_verified_registration_when_challenge_matches(self) -> None:
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        options = verifier.generate_registration_options(
            user_id=b"u", user_name="u", existing_credentials=()
        )
        response = RegistrationResponse(
            credential_id=webauthn_credential_id("cred-abc-123"),
            client_data_challenge=options.challenge,
            public_key=b"fake-public-key-bytes",
            sign_count=0,
            transports=("internal",),
        )
        verified = verifier.verify_registration_response(
            options=options, response=response
        )
        assert isinstance(verified, VerifiedRegistration)
        assert verified.credential_id == webauthn_credential_id("cred-abc-123")
        assert verified.public_key == b"fake-public-key-bytes"
        assert verified.sign_count == 0
        assert verified.transports == ("internal",)

    def test_raises_when_challenge_does_not_match(self) -> None:
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        options = verifier.generate_registration_options(
            user_id=b"u", user_name="u", existing_credentials=()
        )
        response = RegistrationResponse(
            credential_id=webauthn_credential_id("cred-xyz"),
            client_data_challenge=b"wrong-challenge",
            public_key=b"key",
            sign_count=0,
            transports=("internal",),
        )
        with pytest.raises(ValueError, match="challenge"):
            verifier.verify_registration_response(options=options, response=response)


class TestInMemoryAuthenticationOptions:
    def test_returns_options_with_allow_credentials(self) -> None:
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        creds = (
            webauthn_credential_id("cred-a"),
            webauthn_credential_id("cred-b"),
        )
        options = verifier.generate_authentication_options(allow_credentials=creds)
        assert isinstance(options, AuthenticationOptions)
        assert options.allow_credentials == creds
        assert options.challenge != b""
        assert options.relying_party_id == "localhost"


class TestInMemoryVerifyAuthenticationResponse:
    def test_returns_verified_when_challenge_and_credential_match(self) -> None:
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        cred = webauthn_credential_id("cred-a")
        options = verifier.generate_authentication_options(allow_credentials=(cred,))
        response = AuthenticationResponse(
            credential_id=cred,
            client_data_challenge=options.challenge,
            new_sign_count=5,
        )
        verified = verifier.verify_authentication_response(
            options=options,
            response=response,
            stored_public_key=b"stored-key",
            stored_sign_count=0,
        )
        assert isinstance(verified, VerifiedAuthentication)
        assert verified.credential_id == cred
        assert verified.new_sign_count == 5

    def test_raises_when_credential_not_in_allow_list(self) -> None:
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        options = verifier.generate_authentication_options(
            allow_credentials=(webauthn_credential_id("cred-a"),)
        )
        response = AuthenticationResponse(
            credential_id=webauthn_credential_id("cred-not-registered"),
            client_data_challenge=options.challenge,
            new_sign_count=1,
        )
        with pytest.raises(ValueError, match="credential"):
            verifier.verify_authentication_response(
                options=options,
                response=response,
                stored_public_key=b"key",
                stored_sign_count=0,
            )

    def test_raises_when_sign_count_did_not_increase(self) -> None:
        # WebAuthn replay-protection: the new sign_count must be strictly
        # greater than the stored one. The in-memory fake enforces the
        # invariant so tests catch a replay bug.
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        cred = webauthn_credential_id("cred-a")
        options = verifier.generate_authentication_options(allow_credentials=(cred,))
        response = AuthenticationResponse(
            credential_id=cred,
            client_data_challenge=options.challenge,
            new_sign_count=3,
        )
        with pytest.raises(ValueError, match="sign_count"):
            verifier.verify_authentication_response(
                options=options,
                response=response,
                stored_public_key=b"key",
                stored_sign_count=5,
            )
