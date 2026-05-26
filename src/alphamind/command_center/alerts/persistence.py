"""CRUD against the ``alerts`` table (story 05a / ALP-671).

Four operations the engine + route layer use:

* :func:`insert_fired` — append a fresh ``alerts`` row when a rule
  fires. Returns the minted :class:`AlertId`.
* :func:`update_acknowledged` — mark an alert acknowledged. Returns
  ``True`` when a row was updated (idempotent: re-acknowledging a row
  in any post-firing status is a no-op).
* :func:`update_snoozed` — mark an alert snoozed until *snoozed_until*.
  Returns ``True`` when a row was updated.
* :func:`list_active` — return rows with ``status in (firing, snoozed)``
  and (for snoozed) ``snoozed_until > now``. Sort by ``fired_at`` desc
  so the dashboard's banner renders the freshest first.

All four functions take the cc_writer ``async_sessionmaker`` and
acquire a fresh session per call — the route layer's lifespan-bound
factory is the canonical caller. Mirrors the pattern in
:mod:`alphamind.command_center.auth.repository`.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import (
    AlertId,
    AlertRuleName,
    alert_id,
)
from alphamind.command_center.persistence.codecs import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    alert_record_from_row,
)
from alphamind.command_center.persistence.tables import AlertRow

__all__ = [
    "insert_fired",
    "list_active",
    "load_alert",
    "mint_alert_id",
    "update_acknowledged",
    "update_snoozed",
]


def mint_alert_id(now: datetime | None = None) -> AlertId:
    """Mint a fresh alert id.

    Format: ``alert-YYYY-MM-DD-<8-hex>``. Matches the convention named
    in :func:`alphamind.command_center._kernel.ids.alert_id`'s docstring.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")
    return alert_id(f"alert-{stamp}-{secrets.token_hex(4)}")


async def insert_fired(
    factory: async_sessionmaker[AsyncSession],
    *,
    rule_name: AlertRuleName,
    severity: AlertSeverity,
    context_json: str,
    fired_at: datetime,
) -> AlertId:
    """Insert a fresh fired alert row; return the minted alert id.

    The row's ``status`` is :data:`AlertStatus.FIRING` at insertion; the
    operator subsequently transitions it via :func:`update_acknowledged`
    or :func:`update_snoozed`. The ``acknowledged_at`` and
    ``snoozed_until`` columns are NULL at insert.
    """
    new_id = mint_alert_id(fired_at)
    iso = fired_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with factory() as session:
        session.add(
            AlertRow(
                alert_id=new_id,
                rule_name=str(rule_name),
                severity=severity.value,
                status=AlertStatus.FIRING.value,
                fired_at=iso,
                acknowledged_at=None,
                snoozed_until=None,
                context_json=context_json,
            )
        )
        await session.commit()
    return new_id


async def update_acknowledged(
    factory: async_sessionmaker[AsyncSession],
    *,
    alert_id_: AlertId,
    acknowledged_at: datetime,
) -> bool:
    """Mark an alert acknowledged. Returns ``True`` if a row was updated.

    Idempotent on already-acknowledged rows: the WHERE filters to
    ``status = firing`` so a double-ack is a no-op (rowcount = 0 →
    returns False). The operator-facing route surfaces idempotency as
    ``204 No Content`` whether the row was newly acknowledged or already
    was — no exception for "already acked".

    Wraps the row in a fresh transaction; mirrors the pattern in
    :func:`alphamind.command_center.auth.repository.delete_session`.
    """
    iso = acknowledged_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with factory() as session:
        result = await session.execute(
            update(AlertRow)
            .where(
                AlertRow.alert_id == alert_id_,
                AlertRow.status == AlertStatus.FIRING.value,
            )
            .values(
                status=AlertStatus.ACKNOWLEDGED.value,
                acknowledged_at=iso,
            )
        )
        await session.commit()
        rowcount = result.rowcount  # type: ignore[attr-defined]
    return bool(rowcount and rowcount > 0)


async def update_snoozed(
    factory: async_sessionmaker[AsyncSession],
    *,
    alert_id_: AlertId,
    snoozed_until: datetime,
) -> bool:
    """Mark an alert snoozed until *snoozed_until*. Returns ``True`` on update.

    Allowed when the row is currently firing OR already snoozed (an
    operator may extend a snooze). Returns ``False`` when the row is
    acknowledged or missing.
    """
    iso = snoozed_until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with factory() as session:
        result = await session.execute(
            update(AlertRow)
            .where(
                AlertRow.alert_id == alert_id_,
                AlertRow.status.in_(
                    (AlertStatus.FIRING.value, AlertStatus.SNOOZED.value),
                ),
            )
            .values(
                status=AlertStatus.SNOOZED.value,
                snoozed_until=iso,
            )
        )
        await session.commit()
        rowcount = result.rowcount  # type: ignore[attr-defined]
    return bool(rowcount and rowcount > 0)


async def list_active(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> Sequence[AlertRecord]:
    """Return active alerts — firing OR snoozed-with-window-still-open.

    A snoozed alert whose ``snoozed_until`` is in the past is filtered
    out here; the design doc's contract is that the engine re-fires
    those (a fresh row lands once the snooze expires and the
    underlying condition re-evaluates). The dashboard never sees them
    as "active" once the window passes.

    Sort: ``fired_at`` descending so the banner renders the freshest
    first.
    """
    iso = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with factory() as session:
        rows = await session.execute(
            select(AlertRow)
            .where(
                (AlertRow.status == AlertStatus.FIRING.value)
                | ((AlertRow.status == AlertStatus.SNOOZED.value) & (AlertRow.snoozed_until > iso)),
            )
            .order_by(AlertRow.fired_at.desc())
        )
        return [alert_record_from_row(row) for row in rows.scalars().all()]


async def load_alert(
    factory: async_sessionmaker[AsyncSession],
    *,
    alert_id_: AlertId,
) -> AlertRecord | None:
    """Return one alert row by id, or ``None`` when absent.

    Surface used by the route layer's per-alert detail view (story
    06b will reach this) and by the test substrate's after-mutation
    asserts.
    """
    async with factory() as session:
        row = await session.get(AlertRow, alert_id_)
        if row is None:
            return None
        return alert_record_from_row(row)
