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


# NOTE: A full ``_run`` boot smoke test is omitted. ``_run`` hard-codes
# loads from the repo ``_CONFIG_DIR`` for security/alerts, writes a
# ``process_lifetimes`` row (requiring the main Base schema via
# engine_pair_context), and blocks in ``supervisor.run()``. The three
# helper tests above give fast deterministic coverage of the mint+log
# decision and all ACs. The production wiring path is exercised on real
# daemon boots (and would have caught the original gap).
