"""Tests for ``command_center.auth.repository`` (story 03 / ALP-667).

The repository is the imperative shell that owns DB I/O against the two
auth-related tables (``webauthn_credentials`` + ``operator_sessions``).
The route layer calls these functions; tests use the cc_writer session
factory against an in-memory SQLite DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import (
    operator_session_id,
    webauthn_credential_id,
)
from alphamind.command_center.auth.repository import (
    SignCountRaceError,
    count_credentials,
    delete_session,
    insert_credential,
    insert_session,
    list_credentials,
    load_credential,
    load_session,
    update_credential_sign_count,
)
from alphamind.command_center.persistence.codecs import (
    OperatorSessionRecord,
    WebauthnCredentialRecord,
)
from alphamind.command_center.persistence.session import (
    build_cc_writer_session_factory,
)
from alphamind.command_center.persistence.tables import CommandCenterBase


@pytest.fixture
async def cc_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test in-memory async SQLite engine with cc tables created."""
    factory = build_cc_writer_session_factory(":memory:")
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


class TestCredentialCrud:
    async def test_count_credentials_starts_at_zero(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        count = await count_credentials(cc_factory)
        assert count == 0

    async def test_insert_credential_then_load_by_id(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        record = WebauthnCredentialRecord(
            credential_id=webauthn_credential_id("cred-a-1"),
            public_key="pk-bytes-base64url",
            sign_count=0,
            transports="internal",
            created_at="2026-05-26T00:00:00Z",
        )
        await insert_credential(cc_factory, record)
        loaded = await load_credential(cc_factory, credential_id=webauthn_credential_id("cred-a-1"))
        assert loaded == record

    async def test_load_credential_returns_none_when_absent(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        loaded = await load_credential(cc_factory, credential_id=webauthn_credential_id("nope"))
        assert loaded is None

    async def test_count_credentials_after_insert(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        record = WebauthnCredentialRecord(
            credential_id=webauthn_credential_id("cred-x"),
            public_key="pk",
            sign_count=0,
            transports="usb",
            created_at="2026-05-26T00:00:00Z",
        )
        await insert_credential(cc_factory, record)
        assert await count_credentials(cc_factory) == 1

    async def test_list_credentials_returns_all_ids(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for cid in ("cred-1", "cred-2", "cred-3"):
            await insert_credential(
                cc_factory,
                WebauthnCredentialRecord(
                    credential_id=webauthn_credential_id(cid),
                    public_key="pk",
                    sign_count=0,
                    transports="internal",
                    created_at="2026-05-26T00:00:00Z",
                ),
            )
        ids = await list_credentials(cc_factory)
        assert set(ids) == {
            webauthn_credential_id("cred-1"),
            webauthn_credential_id("cred-2"),
            webauthn_credential_id("cred-3"),
        }

    async def test_update_credential_sign_count(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await insert_credential(
            cc_factory,
            WebauthnCredentialRecord(
                credential_id=webauthn_credential_id("cred-sc"),
                public_key="pk",
                sign_count=0,
                transports="internal",
                created_at="2026-05-26T00:00:00Z",
            ),
        )
        await update_credential_sign_count(
            cc_factory,
            credential_id=webauthn_credential_id("cred-sc"),
            new_sign_count=42,
        )
        loaded = await load_credential(cc_factory, credential_id=webauthn_credential_id("cred-sc"))
        assert loaded is not None
        assert loaded.sign_count == 42

    async def test_update_credential_sign_count_rejects_non_increasing(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # F9: strict-increase guard is in the WHERE clause; an update
        # whose new value isn't strictly greater than stored fails.
        await insert_credential(
            cc_factory,
            WebauthnCredentialRecord(
                credential_id=webauthn_credential_id("cred-race"),
                public_key="pk",
                sign_count=10,
                transports="internal",
                created_at="2026-05-26T00:00:00Z",
            ),
        )
        with pytest.raises(SignCountRaceError):
            await update_credential_sign_count(
                cc_factory,
                credential_id=webauthn_credential_id("cred-race"),
                new_sign_count=10,  # equal — must fail
            )
        with pytest.raises(SignCountRaceError):
            await update_credential_sign_count(
                cc_factory,
                credential_id=webauthn_credential_id("cred-race"),
                new_sign_count=5,  # rollback — must fail
            )
        # The stored value is unchanged.
        loaded = await load_credential(
            cc_factory, credential_id=webauthn_credential_id("cred-race")
        )
        assert loaded is not None
        assert loaded.sign_count == 10

    async def test_update_credential_sign_count_concurrent_only_one_wins(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # F9: two concurrent updates against the same credential. The
        # strict-increase WHERE clause + SQLite's per-row WAL semantics
        # serialize the writes; exactly one succeeds.
        import asyncio

        await insert_credential(
            cc_factory,
            WebauthnCredentialRecord(
                credential_id=webauthn_credential_id("cred-concurrent"),
                public_key="pk",
                sign_count=0,
                transports="internal",
                created_at="2026-05-26T00:00:00Z",
            ),
        )

        async def do_update(new_value: int) -> bool:
            try:
                await update_credential_sign_count(
                    cc_factory,
                    credential_id=webauthn_credential_id("cred-concurrent"),
                    new_sign_count=new_value,
                )
            except SignCountRaceError:
                return False
            else:
                return True

        # Both attempt to bump to 5. Exactly one wins. Note: SQLite
        # serializes per-row, and asyncio.gather schedules these in the
        # same event loop, so the "loser" actually re-reads the row
        # after the winner committed; result rowcount is 0 for the
        # loser.
        outcomes = await asyncio.gather(do_update(5), do_update(5))
        assert sum(outcomes) == 1
        # Stored value is exactly 5.
        loaded = await load_credential(
            cc_factory, credential_id=webauthn_credential_id("cred-concurrent")
        )
        assert loaded is not None
        assert loaded.sign_count == 5

    async def test_update_credential_sign_count_missing_row_raises_lookup(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # F9 disambiguation: missing row → LookupError, not SignCountRaceError.
        with pytest.raises(LookupError):
            await update_credential_sign_count(
                cc_factory,
                credential_id=webauthn_credential_id("cred-not-there"),
                new_sign_count=1,
            )


class TestSessionCrud:
    async def test_insert_session_then_load_by_id(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Sessions FK into credentials — seed a credential first.
        await insert_credential(
            cc_factory,
            WebauthnCredentialRecord(
                credential_id=webauthn_credential_id("cred-s"),
                public_key="pk",
                sign_count=0,
                transports="internal",
                created_at="2026-05-26T00:00:00Z",
            ),
        )
        sess = OperatorSessionRecord(
            session_id=operator_session_id("sess-1"),
            credential_id=webauthn_credential_id("cred-s"),
            expires_at="2026-05-26T12:00:00Z",
            csrf_token_hash="hashvalue-deadbeef",
            created_at="2026-05-26T00:00:00Z",
        )
        await insert_session(cc_factory, sess)
        loaded = await load_session(cc_factory, session_id=operator_session_id("sess-1"))
        assert loaded == sess

    async def test_load_session_returns_none_when_absent(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        loaded = await load_session(cc_factory, session_id=operator_session_id("nope"))
        assert loaded is None

    async def test_delete_session_removes_row(
        self, cc_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await insert_credential(
            cc_factory,
            WebauthnCredentialRecord(
                credential_id=webauthn_credential_id("cred-d"),
                public_key="pk",
                sign_count=0,
                transports="internal",
                created_at="2026-05-26T00:00:00Z",
            ),
        )
        await insert_session(
            cc_factory,
            OperatorSessionRecord(
                session_id=operator_session_id("sess-d"),
                credential_id=webauthn_credential_id("cred-d"),
                expires_at="2026-05-26T12:00:00Z",
                csrf_token_hash="h",
                created_at="2026-05-26T00:00:00Z",
            ),
        )
        await delete_session(cc_factory, session_id=operator_session_id("sess-d"))
        loaded = await load_session(cc_factory, session_id=operator_session_id("sess-d"))
        assert loaded is None
