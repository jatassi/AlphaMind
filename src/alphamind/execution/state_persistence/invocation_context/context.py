"""Per-invocation context primitives (ALP-449 three-tx model).

Three pieces:

* :class:`InvocationHandle` — the immutable-identity + open-session pair
  that downstream write paths consume. The orchestrator opens one session
  per phase and binds it to a fresh handle; the handle is *not* shared
  across phase boundaries.
* :func:`insert_invocation_row` — the "Step 0" of the three-transaction
  model. Inserts the supplied :class:`InvocationRecord` in its own
  short-lived transaction and commits before Phase 1 begins, so the
  invocation row is durable + visible to fresh-session reads from the
  moment Phase 1 starts.
* :class:`InvocationContext` — convenience async context manager bundling
  ``insert_invocation_row`` + one phase's session. **For tests and verify
  scripts that scope one phase's work to a single ``async with`` block.**
  Production orchestrator code opens its per-phase sessions explicitly so
  the three-transaction sequencing stays visible at the call site (see
  ``alphamind.scheduler.orchestrator.run_invocation``).

The class's behaviour differs from the pre-ALP-449 ``InvocationContext``
in one important way: the invocation row is committed in its own short
transaction *before* the phase session opens, so an exception inside the
``async with`` body rolls back **only the phase's writes**, never the
row itself. This matches the design's snapshot-isolation contract in
``docs/design/05-execution-layer/state-persistence.md`` § Snapshot
isolation.

:func:`stamp_phase_completion` continues to set the row's phase-completion
columns; it runs inside the calling phase's open session so the stamp
participates in that phase's commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.execution.state_persistence.invocation_context.records import (
    InvocationRecord,
    invocation_record_to_row,
)
from alphamind.execution.state_persistence.tables.invocations import InvocationRow


@dataclass(frozen=True)
class InvocationHandle:
    """Per-phase binding of an open session to an invocation identity.

    The orchestrator builds a fresh handle for each phase's transaction
    (Phase 1, Phase 2) — the handle is *not* shared across phase
    boundaries; the invocation_id is, but each phase's session is its
    own.
    """

    session: AsyncSession
    invocation_id: str


async def insert_invocation_row(
    session_factory: async_sessionmaker[AsyncSession],
    record: InvocationRecord,
) -> None:
    """Insert one ``invocations`` row in its own short transaction; commit.

    The row is durable in the DB on return — Phase 1's transaction opens
    afterwards and can read the row from a fresh session; the SQL
    repository's snapshot-isolation guard (``phase1_completed_at IS NULL``
    → ``RepositoryConsistencyError``) can then participate correctly
    across phase boundaries.

    Raises ``IntegrityError`` if the FK to ``process_lifetimes`` does not
    resolve (or any other constraint fires). Callers propagate.
    """
    async with session_factory() as session:
        session.add(invocation_record_to_row(record))
        await session.commit()


class InvocationContext:
    """Async context manager: row commit + one phase session.

    Convenience wrapper for tests and verify scripts whose pre-ALP-449
    shape was a single ``async with InvocationContext(...) as handle:``
    block bounding one phase's writes. ``__aenter__`` calls
    :func:`insert_invocation_row` to commit the invocation row in its own
    short transaction, then opens a fresh phase session. ``__aexit__``
    commits the phase session on clean exit; on exception, rolls back the
    phase's writes only — the invocation row stays (separate transaction
    boundary, by design).

    Production orchestrator code does **not** use this helper; it opens
    its per-phase sessions explicitly so the three-transaction sequencing
    is visible at the call site. See
    :func:`alphamind.scheduler.orchestrator.run_invocation`.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        record: InvocationRecord,
    ) -> None:
        self._session_factory = session_factory
        self._record = record
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> InvocationHandle:
        await insert_invocation_row(self._session_factory, self._record)
        self._session = self._session_factory()
        return InvocationHandle(session=self._session, invocation_id=self._record.invocation_id)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None


_PhaseColumn = Literal["phase1_completed_at", "phase2_completed_at"]


async def stamp_phase_completion(handle: InvocationHandle, *, column: _PhaseColumn) -> None:
    """Set the bound invocation row's phase-completion column to now (UTC).

    Phase 1 / Phase 2 write paths call this as the final step inside the
    phase's open session so the surrounding commit flips the row from
    "in flight" to "committed". The SQL repository's snapshot-isolation
    guard reads ``phase1_completed_at`` and raises
    ``RepositoryConsistencyError`` when it remains NULL.
    """
    row = await handle.session.get(InvocationRow, handle.invocation_id)
    if row is None:
        msg = (
            f"invocations row {handle.invocation_id!r} disappeared mid-transaction; "
            "insert_invocation_row should have inserted it before this phase opened"
        )
        raise RuntimeError(msg)
    setattr(row, column, datetime.now(UTC).isoformat().replace("+00:00", "Z"))
