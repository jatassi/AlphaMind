"""Auth-related DB operations (story 03 / ALP-667).

The imperative shell that owns DB I/O against the two auth-related
tables (``webauthn_credentials`` + ``operator_sessions``). All
functions take the cc_writer ``async_sessionmaker`` and acquire a
fresh session each call — the route layer's lifespan-bound factory is
the canonical caller.

Pure functions over a session factory; no shared state. The route
layer's TDD-driven tests exercise these directly, and the integration
tests against the FastAPI TestClient exercise them through the route
surface.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import (
    OperatorSessionId,
    WebauthnCredentialId,
)
from alphamind.command_center.persistence.codecs import (
    OperatorSessionRecord,
    WebauthnCredentialRecord,
    operator_session_record_from_row,
    operator_session_record_to_row,
    webauthn_credential_record_from_row,
    webauthn_credential_record_to_row,
)
from alphamind.command_center.persistence.tables import (
    OperatorSessionRow,
    WebauthnCredentialRow,
)

__all__ = [
    "count_credentials",
    "delete_session",
    "insert_credential",
    "insert_session",
    "list_credentials",
    "load_credential",
    "load_session",
    "update_credential_sign_count",
]


async def count_credentials(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    """Return the number of rows in ``webauthn_credentials``.

    The setup-token gate consults this: a zero count unlocks the
    first-registration path; a positive count locks it (subsequent
    registrations require an existing-session bearer).
    """
    async with factory() as session:
        rows = await session.execute(select(WebauthnCredentialRow.credential_id))
        return len(list(rows.scalars()))


async def list_credentials(
    factory: async_sessionmaker[AsyncSession],
) -> Sequence[WebauthnCredentialId]:
    """Return all registered credential IDs.

    The route layer's ``/auth/login/begin`` uses this to populate the
    ``allow_credentials`` field of the authentication options.
    """
    async with factory() as session:
        rows = await session.execute(select(WebauthnCredentialRow))
        return [
            webauthn_credential_record_from_row(row).credential_id
            for row in rows.scalars().all()
        ]


async def insert_credential(
    factory: async_sessionmaker[AsyncSession],
    record: WebauthnCredentialRecord,
) -> None:
    """Persist a new credential record to ``webauthn_credentials``."""
    async with factory() as session:
        session.add(webauthn_credential_record_to_row(record))
        await session.commit()


async def load_credential(
    factory: async_sessionmaker[AsyncSession],
    *,
    credential_id: WebauthnCredentialId,
) -> WebauthnCredentialRecord | None:
    """Return the credential record for *credential_id* or ``None``."""
    async with factory() as session:
        row = await session.get(WebauthnCredentialRow, credential_id)
        if row is None:
            return None
        return webauthn_credential_record_from_row(row)


async def update_credential_sign_count(
    factory: async_sessionmaker[AsyncSession],
    *,
    credential_id: WebauthnCredentialId,
    new_sign_count: int,
) -> None:
    """Update the ``sign_count`` for a credential after a successful login.

    Raises :class:`LookupError` if the credential row is absent — the
    route layer's verify path has already confirmed it exists, so a
    missing row here indicates a concurrent revocation.
    """
    async with factory() as session:
        row = await session.get(WebauthnCredentialRow, credential_id)
        if row is None:
            msg = f"credential {credential_id!r} not found for sign_count update"
            raise LookupError(msg)
        row.sign_count = new_sign_count
        await session.commit()


async def insert_session(
    factory: async_sessionmaker[AsyncSession],
    record: OperatorSessionRecord,
) -> None:
    """Persist a new session record to ``operator_sessions``."""
    async with factory() as session:
        session.add(operator_session_record_to_row(record))
        await session.commit()


async def load_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    session_id: OperatorSessionId,
) -> OperatorSessionRecord | None:
    """Return the session record for *session_id* or ``None``."""
    async with factory() as session:
        row = await session.get(OperatorSessionRow, session_id)
        if row is None:
            return None
        return operator_session_record_from_row(row)


async def delete_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    session_id: OperatorSessionId,
) -> None:
    """Delete a session row by id.

    Idempotent: deleting an absent row is a no-op so /auth/logout can
    return 204 regardless of whether the row was still present.
    """
    async with factory() as session:
        row = await session.get(OperatorSessionRow, session_id)
        if row is None:
            return
        await session.delete(row)
        await session.commit()
