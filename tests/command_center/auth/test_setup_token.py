"""Tests for ``command_center.auth.setup_token`` (story 03 / ALP-667).

The setup-token gate: when ``webauthn_credentials`` has zero rows, the
``/auth/register/begin`` accepts a one-time setup token (printed to
stdout at daemon first launch); after the first credential lands, the
token path locks out and subsequent registrations require an existing-
session bearer.

The :class:`SetupTokenGate` carries one secret in memory; tests against
the gate exercise mint + consume + locked semantics without DB I/O.
The route-layer test for the credential-count gate is in ``test_routes.py``.
"""

from __future__ import annotations

import pytest

from alphamind.command_center.auth.setup_token import (
    SetupTokenAlreadyConsumed,
    SetupTokenGate,
    SetupTokenMismatch,
)


class TestSetupTokenGateLifecycle:
    def test_mint_returns_non_empty_url_safe_token(self) -> None:
        gate = SetupTokenGate()
        token = gate.mint()
        assert token
        assert all(c.isalnum() or c in "-_" for c in token)

    def test_each_mint_returns_a_fresh_token(self) -> None:
        gate = SetupTokenGate()
        assert gate.mint() != gate.mint()

    def test_consume_with_matching_token_succeeds(self) -> None:
        gate = SetupTokenGate()
        token = gate.mint()
        # Consume returns nothing on success; raises on failure.
        gate.consume(token)
        # After consume, the gate is locked.
        assert gate.is_consumed()

    def test_consume_with_wrong_token_raises_mismatch(self) -> None:
        gate = SetupTokenGate()
        gate.mint()
        with pytest.raises(SetupTokenMismatch):
            gate.consume("wrong-token")

    def test_consume_twice_raises_already_consumed(self) -> None:
        gate = SetupTokenGate()
        token = gate.mint()
        gate.consume(token)
        with pytest.raises(SetupTokenAlreadyConsumed):
            gate.consume(token)

    def test_is_consumed_starts_false(self) -> None:
        gate = SetupTokenGate()
        assert not gate.is_consumed()
        gate.mint()
        assert not gate.is_consumed()

    def test_mint_after_consume_raises_already_consumed(self) -> None:
        # Once consumed, the gate cannot mint a fresh token — the
        # operator must instead authenticate with an existing session
        # to register additional credentials.
        gate = SetupTokenGate()
        token = gate.mint()
        gate.consume(token)
        with pytest.raises(SetupTokenAlreadyConsumed):
            gate.mint()
