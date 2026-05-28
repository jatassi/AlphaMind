"""Tests for ``command_center.auth.webauthn`` (story 03 / ALP-667).

Covers the :class:`WebauthnVerifier` Protocol surface and its two
implementations:

* :class:`InMemoryWebauthnVerifier` — deterministic fake used by unit
  tests. Returns canned challenges and accepts any response whose
  ``credential_id`` matches.
* :class:`RealWebauthnVerifier` — wraps the four ``py_webauthn`` calls.
  The py-webauthn translation seam (rawId / credential dict construction)
  is exercised by the new seam tests; full ceremonies against real
  authenticators remain the verify script's (story 07) responsibility.

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
    RealWebauthnVerifier,
    RegistrationOptions,
    RegistrationResponse,
    VerifiedAuthentication,
    VerifiedRegistration,
    WebauthnVerifier,
    encode_inmemory_attestation_object,
    encode_inmemory_client_data_json,
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
            client_data_json=encode_inmemory_client_data_json(options.challenge),
            attestation_object=encode_inmemory_attestation_object(
                public_key=b"fake-public-key-bytes", sign_count=0
            ),
            transports=("internal",),
        )
        verified = verifier.verify_registration_response(options=options, response=response)
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
            client_data_json=encode_inmemory_client_data_json(b"wrong-challenge"),
            attestation_object=encode_inmemory_attestation_object(public_key=b"key", sign_count=0),
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
            client_data_json=encode_inmemory_client_data_json(options.challenge),
            authenticator_data=b"fake-auth-data",
            signature=b"fake-signature",
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
            client_data_json=encode_inmemory_client_data_json(options.challenge),
            authenticator_data=b"fake-auth-data",
            signature=b"fake-signature",
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
        # greater than the stored one — but only when EITHER is non-zero
        # (counter-not-supported authenticators report 0 forever).
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        cred = webauthn_credential_id("cred-a")
        options = verifier.generate_authentication_options(allow_credentials=(cred,))
        response = AuthenticationResponse(
            credential_id=cred,
            client_data_json=encode_inmemory_client_data_json(options.challenge),
            authenticator_data=b"fake-auth-data",
            signature=b"fake-signature",
            new_sign_count=3,
        )
        with pytest.raises(ValueError, match="sign_count"):
            verifier.verify_authentication_response(
                options=options,
                response=response,
                stored_public_key=b"key",
                stored_sign_count=5,
            )

    def test_accepts_sign_count_zero_when_stored_is_also_zero(self) -> None:
        # Authenticators that don't support counters report 0 forever.
        # py_webauthn accepts 0/0; the in-memory fake mirrors that so a
        # counter-less authenticator can log in successfully.
        verifier = InMemoryWebauthnVerifier(relying_party_id="localhost")
        cred = webauthn_credential_id("cred-no-counter")
        options = verifier.generate_authentication_options(allow_credentials=(cred,))
        response = AuthenticationResponse(
            credential_id=cred,
            client_data_json=encode_inmemory_client_data_json(options.challenge),
            authenticator_data=b"fake-auth-data",
            signature=b"fake-signature",
            new_sign_count=0,
        )
        verified = verifier.verify_authentication_response(
            options=options,
            response=response,
            stored_public_key=b"key",
            stored_sign_count=0,
        )
        assert verified.new_sign_count == 0


# ---------------------------------------------------------------------------
# RealWebauthnVerifier seam tests (ALP-723 regression guard)
# These directly exercise the py-webauthn translation layer that the
# InMemory fake never touches. The double-base64url rawId bug only
# manifested here.
# ---------------------------------------------------------------------------


class TestRealWebauthnVerifierRawIdEncoding:
    """Guard against re-introducing the ALP-723 double-encoding of rawId.

    The ``credential_id`` in RegistrationResponse/AuthenticationResponse
    (and thus the value from the SPA) is *already* base64url. Both "id"
    and "rawId" passed to py-webauthn must be identical strings.
    """

    def test_registration_does_not_double_encode_raw_id(self) -> None:
        verifier = RealWebauthnVerifier(
            relying_party_id="localhost",
            relying_party_name="Test RP",
            expected_origin="http://localhost:8080",
        )
        options = verifier.generate_registration_options(
            user_id=b"test-operator",
            user_name="test",
            existing_credentials=(),
        )
        # Dummy blobs suffice: id/rawId check is first in py-webauthn.
        response = RegistrationResponse(
            credential_id=webauthn_credential_id("cred-723"),
            client_data_json=b"{}",
            attestation_object=b"{}",
            transports=(),
        )
        try:
            verifier.verify_registration_response(options=options, response=response)
        except Exception as exc:
            msg = str(exc)
            assert "id and raw_id were not equivalent" not in msg, (
                "ALP-723 regression: rawId double-encoded on registration path"
            )
            # Any later error (bad dummy clientDataJSON etc.) is fine;
            # reaching here means the rawId seam passed.

    def test_authentication_does_not_double_encode_raw_id(self) -> None:
        verifier = RealWebauthnVerifier(
            relying_party_id="localhost",
            relying_party_name="Test RP",
            expected_origin="http://localhost:8080",
        )
        cred = webauthn_credential_id("cred-723")
        options = verifier.generate_authentication_options(allow_credentials=(cred,))
        response = AuthenticationResponse(
            credential_id=cred,
            client_data_json=b"{}",
            authenticator_data=b"{}",
            signature=b"{}",
            new_sign_count=1,
        )
        try:
            verifier.verify_authentication_response(
                options=options,
                response=response,
                stored_public_key=b"fake-pk-for-test",
                stored_sign_count=0,
            )
        except Exception as exc:
            msg = str(exc)
            assert "id and raw_id were not equivalent" not in msg, (
                "ALP-723 regression: rawId double-encoded on authentication path"
            )
            # Any later error is fine for the seam test.
