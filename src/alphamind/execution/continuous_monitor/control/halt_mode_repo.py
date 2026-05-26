"""Halt-mode state repository for the monitor (ALP-665).

The ``POST /control/set_halt_mode`` verb mutates the operator-set halt-mode
flag and persists it to the ``monitor_halt_mode`` singleton table so the
value survives monitor restarts — the ``halt_mode_engaged`` portfolio state
field per ``docs/design/monitor-control-and-events-schema.md`` § Notes on
cross-field invariants.

Per-process discipline: one repository instance per monitor session, sharing
the daemon's async session factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.state.tables.monitor_halt_mode import (
    MONITOR_HALT_MODE_SINGLETON_ID,
    MonitorHaltModeRow,
)


@dataclass(frozen=True, slots=True)
class HaltModeRecord:
    """Typed view over the singleton row.

    ``enabled`` mirrors the wire-format ``boolean``; ``reason`` and
    ``applied_at`` are ``None`` on the disengaged-default boot path so the
    verb cannot accidentally infer a never-set value as authoritative.
    """

    enabled: bool
    reason: str | None
    applied_at: datetime | None


class HaltModeRepository:
    """Read / write wrapper over ``monitor_halt_mode``.

    The repository opens a fresh ``AsyncSession`` per read/write and commits
    inline — same shape as the activity-log emitter wiring. This keeps the
    transaction narrow (one row, one commit) and avoids holding the DB lock
    across the verb's external surface (envelope synthesis, OMS submit).
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(self) -> HaltModeRecord:
        """Return the current halt-mode state, or the disengaged default.

        Snapshots the row attributes into plain Python values inside the
        session block so the returned :class:`HaltModeRecord` does not hold
        a reference to a possibly-detached SQLAlchemy instance (F13).
        """
        async with self._session_factory() as session:
            stmt = select(MonitorHaltModeRow).where(
                MonitorHaltModeRow.id == MONITOR_HALT_MODE_SINGLETON_ID
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return HaltModeRecord(enabled=False, reason=None, applied_at=None)
            # Materialize attributes inside the session before exit so we
            # never reach for ORM state on a detached instance.
            enabled = bool(row.enabled)
            reason = row.reason
            applied_at_str = row.applied_at
        applied_at: datetime | None = None
        if applied_at_str is not None:
            applied_at = _parse_iso_z(applied_at_str)
        return HaltModeRecord(enabled=enabled, reason=reason, applied_at=applied_at)

    async def write(self, record: HaltModeRecord) -> None:
        """Upsert the singleton row from *record*.

        Uses SQLite's ``INSERT ... ON CONFLICT(id) DO UPDATE`` so the write
        is a single atomic statement — eliminates the prior select-then-insert
        TOCTOU race on the singleton row. Two concurrent writes both reach the
        same final-state row without IntegrityError.

        Preserves idempotent-on-same-value semantics: if the new
        ``(enabled, reason)`` equals the existing row, the existing
        ``applied_at`` is preserved rather than overwritten with the
        request's timestamp. This keeps the operator-facing "applied at"
        anchored to the actual state transition, not to subsequent
        confirmations of the same state.
        """
        applied_at_iso = (
            _datetime_to_iso_z(record.applied_at) if record.applied_at is not None else None
        )
        enabled_int = 1 if record.enabled else 0
        async with self._session_factory() as session:
            stmt = sqlite_insert(MonitorHaltModeRow).values(
                id=MONITOR_HALT_MODE_SINGLETON_ID,
                enabled=enabled_int,
                reason=record.reason,
                applied_at=applied_at_iso,
            )
            # Idempotent-on-same-value: preserve the existing applied_at when
            # the new enabled/reason match the existing row. SQLite's
            # ``excluded`` pseudo-row carries the proposed-insert values; the
            # CASE picks between the existing applied_at (no-op confirmation)
            # and excluded.applied_at (real transition).
            upsert = stmt.on_conflict_do_update(
                index_elements=[MonitorHaltModeRow.id],
                set_={
                    "enabled": stmt.excluded.enabled,
                    "reason": stmt.excluded.reason,
                    "applied_at": case(
                        (
                            (MonitorHaltModeRow.enabled == stmt.excluded.enabled)
                            & (
                                func.coalesce(MonitorHaltModeRow.reason, literal(""))
                                == func.coalesce(stmt.excluded.reason, literal(""))
                            ),
                            MonitorHaltModeRow.applied_at,
                        ),
                        else_=stmt.excluded.applied_at,
                    ),
                },
            )
            await session.execute(upsert)
            await session.commit()


def _datetime_to_iso_z(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"applied_at must be timezone-aware; got {value!r}"
        raise ValueError(msg)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso_z(value: str) -> datetime:
    # ``fromisoformat`` accepts ``Z`` from Python 3.11 onward, but the codebase
    # has historically normalized to ``+00:00`` for safety on older runners.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


__all__ = [
    "HaltModeRecord",
    "HaltModeRepository",
]
