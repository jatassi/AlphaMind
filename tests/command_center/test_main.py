"""Tests for ``alphamind.command_center.__main__`` boot wiring (ALP-721).

These cover the first-launch setup-token mint + log behavior that the
production ``_run`` path must perform (the gap that blocked initial
passkey enrollment on fresh installs). The helper is exercised directly
for fast deterministic unit coverage of the ACs.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center.__main__ import _maybe_emit_setup_token
from alphamind.command_center._kernel.ids import WebauthnCredentialId
from alphamind.command_center.auth.repository import insert_credential
from alphamind.command_center.auth.setup_token import (
    SetupTokenAlreadyConsumedError,
    SetupTokenGate,
)
from alphamind.command_center.persistence.codecs import WebauthnCredentialRecord
from alphamind.command_center.persistence.session import build_cc_writer_session_factory
from alphamind.command_center.persistence.tables import CommandCenterBase


@pytest.fixture
async def cc_factory() -> async_sessionmaker[AsyncSession]:
    """In-memory cc_writer factory with tables created (mirrors auth/conftest)."""
    factory = build_cc_writer_session_factory(":memory:")
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    return factory


@pytest.mark.asyncio
class TestMaybeEmitSetupToken:
    async def test_mints_and_logs_token_when_zero_credentials(
        self, cc_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC1 + AC3: empty DB → mints, logs the "command_center setup token: <value>" line, token matches gate state."""  # noqa: E501
        gate = SetupTokenGate()
        caplog.set_level(logging.INFO, logger="alphamind.command_center.__main__")

        token = await _maybe_emit_setup_token(
            gate=gate,
            cc_writer_factory=cc_factory,
            logger=logging.getLogger("alphamind.command_center.__main__"),
        )

        assert token is not None
        assert len(token) > 20  # urlsafe 32 bytes -> ~43 chars
        assert f"command_center setup token: {token}" in caplog.text
        assert gate._token == token  # operator's pasted value will match verify()

    async def test_logs_not_minted_when_credentials_exist(
        self, cc_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC2: existing credential → logs the 'not minted (N existing credentials)' form, does not mint."""  # noqa: E501
        # Seed one credential (tables created by fixture).
        rec = WebauthnCredentialRecord(
            credential_id=WebauthnCredentialId("seed-cred-1"),
            public_key="fake-pubkey-base64url",
            sign_count=0,
            transports="",
            created_at="2026-05-28T00:00:00Z",
        )
        await insert_credential(cc_factory, rec)

        gate = SetupTokenGate()
        caplog.set_level(logging.INFO, logger="alphamind.command_center.__main__")

        token = await _maybe_emit_setup_token(
            gate=gate,
            cc_writer_factory=cc_factory,
            logger=logging.getLogger("alphamind.command_center.__main__"),
        )

        assert token is None
        assert gate._token is None
        assert "command_center setup token: not minted (1 existing credentials)" in caplog.text
        assert "command_center setup token:" in caplog.text  # the not-minted variant

    async def test_minted_token_is_consumable_by_gate_verify(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The value returned (and logged) is exactly what gate.verify accepts before lock."""
        gate = SetupTokenGate()

        token = await _maybe_emit_setup_token(
            gate=gate,
            cc_writer_factory=cc_factory,
            logger=logging.getLogger("alphamind.command_center.__main__"),
        )

        assert token is not None
        # Should not raise
        gate.verify(token)
        gate.lock()
        # After lock, further mint raises (gate consumed)
        with pytest.raises(SetupTokenAlreadyConsumedError):
            gate.mint()

    async def test_emits_lan_hostname_suggestion_on_nonlocal_bind_zero_creds(
        self, cc_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """ALP-727 AC1+AC2+AC4: non-local bind + zero creds → mints token then emits the exact LAN suggestion block (after token line) containing hostname suggestion, YAML snippets, RUNBOOK ptr, and socket.gethostname() mention."""  # noqa: E501
        gate = SetupTokenGate()
        caplog.set_level(logging.INFO, logger="alphamind.command_center.__main__")

        token = await _maybe_emit_setup_token(
            gate=gate,
            cc_writer_factory=cc_factory,
            logger=logging.getLogger("alphamind.command_center.__main__"),
            bind_host="192.168.1.100",
        )

        assert token is not None
        assert f"command_center setup token: {token}" in caplog.text
        # Suggestion appears after the token line (same caplog.text sequence)
        token_pos = caplog.text.find(f"command_center setup token: {token}")
        sugg_pos = caplog.text.find("LAN / hostname suggestion for passkey support:")
        assert token_pos != -1
        assert sugg_pos != -1
        assert token_pos < sugg_pos
        # Exact block contents per story (AC1, AC2)
        assert "Edit config/command-center.yaml:" in caplog.text
        assert 'scheme: "http"' in caplog.text
        assert 'host: "' in caplog.text and ".local" in caplog.text
        assert "Edit config/security.yaml:" in caplog.text
        assert 'relying_party_id: "' in caplog.text
        assert "socket.gethostname() ==" in caplog.text  # derives realistic suggestion
        assert "RUNBOOK_command_center.md § LAN access for the full recipe." in caplog.text

    async def test_no_lan_suggestion_on_localhost_bind(
        self, cc_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """ALP-727 AC3: localhost bind (even with zero creds) → no LAN suggestion block emitted."""
        gate = SetupTokenGate()
        caplog.set_level(logging.INFO, logger="alphamind.command_center.__main__")

        token = await _maybe_emit_setup_token(
            gate=gate,
            cc_writer_factory=cc_factory,
            logger=logging.getLogger("alphamind.command_center.__main__"),
            bind_host="127.0.0.1",
        )

        assert token is not None
        assert f"command_center setup token: {token}" in caplog.text
        assert "LAN / hostname suggestion for passkey support:" not in caplog.text

    async def test_no_lan_suggestion_when_credentials_exist_even_on_nonlocal_bind(
        self, cc_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """ALP-727 AC3: creds exist + non-local bind → no suggestion (and no mint)."""
        rec = WebauthnCredentialRecord(
            credential_id=WebauthnCredentialId("seed-lan-1"),
            public_key="fake-pubkey-base64url",
            sign_count=0,
            transports="",
            created_at="2026-05-28T00:00:00Z",
        )
        await insert_credential(cc_factory, rec)

        gate = SetupTokenGate()
        caplog.set_level(logging.INFO, logger="alphamind.command_center.__main__")

        token = await _maybe_emit_setup_token(
            gate=gate,
            cc_writer_factory=cc_factory,
            logger=logging.getLogger("alphamind.command_center.__main__"),
            bind_host="10.0.0.5",
        )

        assert token is None
        assert "LAN / hostname suggestion for passkey support:" not in caplog.text
        assert "command_center setup token: not minted" in caplog.text


# NOTE: A full ``_run`` boot smoke test is omitted. ``_run`` hard-codes
# loads from the repo ``_CONFIG_DIR`` for security/alerts, writes a
# ``process_lifetimes`` row (requiring the main Base schema via
# engine_pair_context), and blocks in ``supervisor.run()``. The three
# helper tests above give fast deterministic coverage of the mint+log
# decision and all ACs. The production wiring path is exercised on real
# daemon boots (and would have caught the original gap).
