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

from sqlalchemy import select
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
        """Return the current halt-mode state, or the disengaged default."""
        async with self._session_factory() as session:
            stmt = select(MonitorHaltModeRow).where(
                MonitorHaltModeRow.id == MONITOR_HALT_MODE_SINGLETON_ID
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return HaltModeRecord(enabled=False, reason=None, applied_at=None)
        applied_at: datetime | None = None
        if row.applied_at is not None:
            applied_at = _parse_iso_z(row.applied_at)
        return HaltModeRecord(
            enabled=bool(row.enabled), reason=row.reason, applied_at=applied_at
        )

    async def write(self, record: HaltModeRecord) -> None:
        """Upsert the singleton row from *record*."""
        applied_at_iso = (
            _datetime_to_iso_z(record.applied_at) if record.applied_at is not None else None
        )
        async with self._session_factory() as session:
            stmt = select(MonitorHaltModeRow).where(
                MonitorHaltModeRow.id == MONITOR_HALT_MODE_SINGLETON_ID
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = MonitorHaltModeRow(
                    id=MONITOR_HALT_MODE_SINGLETON_ID,
                    enabled=1 if record.enabled else 0,
                    reason=record.reason,
                    applied_at=applied_at_iso,
                )
                session.add(row)
            else:
                row.enabled = 1 if record.enabled else 0
                row.reason = record.reason
                row.applied_at = applied_at_iso
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
