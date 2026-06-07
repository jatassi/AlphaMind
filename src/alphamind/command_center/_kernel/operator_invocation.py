"""``OperatorInvocationHandle`` primitive (story 02 / ALP-666 — pre-resolved D).

Every ``/api/control/*`` verb the command center proxies (story 04a) opens
a short-lived synthetic :class:`InvocationHandle` for the duration of the
verb dispatch. The handle is the binding scope for any activity-log
entries the verb emits — the audit trail then carries the
``invocation_id`` so the activity-log explorer can filter operator
actions per the "Operator actions" saved filter view (per the design
doc's § Audit).

Design contract:

* The yielded handle is the *standard* :class:`InvocationHandle` from
  :mod:`alphamind.state.invocation_context.context` — operator-action
  invocations and pipeline invocations share the same row shape so the
  activity-log binding semantics are identical.
* The invocation row's ``trigger_source`` is set to
  :data:`RUN_TYPE_OPERATOR_CONSOLE` (the existing ``invocations`` schema
  carries trigger-source on the row already; there is no separate
  ``run_type`` column, and ``trigger_source`` is the schema's contract
  for "where did this trigger come from").
* The invocation row's ``trigger_reason`` carries the verb name + the
  operator session ID as a structured ``key=value`` string so the
  activity-log explorer can grep them out without joining other tables.
* The invocation row is committed in its own short transaction before
  the body runs (mirroring :func:`insert_invocation_row`'s three-tx
  model); a caller-raised exception inside the body rolls back the
  phase session's writes, never the row itself.

Many fields on :class:`InvocationRecord` (``git_sha_at_invocation``,
``active_profile``, ``active_regime``, ``active_mode``,
``active_overlays_json``, the resolved-config + calibration-state
snapshot paths) carry pipeline-only semantics. For operator-action
invocations the helper fills them with sentinel strings that the
activity-log explorer treats as "not applicable"; the row remains
schema-valid because every ``InvocationRecord`` field is non-NULL.
Story 04a's proxy is free to override these via the
``snapshot_overrides`` parameter when it has richer context (e.g. the
most recent pipeline invocation's resolved-config-hash).

The session factory threaded in is the **production**
``Base``-backed async session factory (built by
:func:`alphamind.persistence.session.engine_pair_context`), NOT the
command-center writer factory. :class:`InvocationRow` lives on the
production ``Base.metadata``; the cc writer's ``before_flush`` guard
would reject the row because its table is not in the cc-owned allow-list.
``operator_invocation`` IS a legitimate cross-package write (it bridges
an operator action into an invocation row) — by design it bypasses the
cc writer and uses the production factory the composition root already
holds.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.control import ControlVerb
from alphamind.command_center._kernel.ids import OperatorSessionId
from alphamind.state.invocation_context.context import (
    InvocationHandle,
    insert_invocation_row,
)
from alphamind.state.invocation_context.records import InvocationRecord

__all__ = ["RUN_TYPE_OPERATOR_CONSOLE", "operator_invocation"]

log = logging.getLogger(__name__)


RUN_TYPE_OPERATOR_CONSOLE = "operator_console"
"""Sentinel value the operator-invocation context manager writes to
``invocations.trigger_source``.

Mirrors the activity-log convention where ``source: operator_console``
identifies state mutations the command center initiated (per the
design doc's § Operator actions). The activity-log explorer's "Operator
actions" saved filter view joins ``activity_log`` rows against
``invocations`` rows whose ``trigger_source`` equals this value.
"""

_OPERATOR_CONSOLE_SENTINEL = "operator_action"
"""Sentinel filler for the pipeline-only fields on the invocation row.

The schema requires every ``InvocationRecord`` field to be non-NULL; an
operator action has no resolved-config snapshot, no calibration-state
snapshot path, no active profile / regime / mode in the usual sense.
The sentinel lets the row stay schema-valid while flagging "not
applicable" to any downstream reader that bothers to inspect.
"""

_EMPTY_JSON = "{}"
"""Empty JSON object — used as the sentinel for the JSON-blob fields on
the invocation row (``active_overlays_json`` /
``feature_flags_snapshot_json`` / ``data_source_freshness_json``).
"""


def _mint_invocation_id(now: datetime) -> str:
    """Mint a fresh invocation id matching the pipeline's convention.

    Format: ``inv-YYYYMMDDTHHMMSSZ-<8-hex>``. Mirrors
    :func:`alphamind.scheduler.invocation._mint_invocation_id` so an
    operator-action row sorts alongside pipeline-invocation rows on
    timestamp prefix.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"inv-{stamp}-{secrets.token_hex(4)}"


def _format_now(now: datetime) -> str:
    """Format a tz-aware UTC datetime as ``YYYY-MM-DDTHH:MM:SSZ``.

    Matches the rest of the state-persistence layer's ISO-8601 convention
    (terminal ``Z`` rather than ``+00:00``).
    """
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_record(
    *,
    invocation_id: str,
    process_lifetime_id: str,
    operator_session_id_: OperatorSessionId,
    verb: ControlVerb,
    now: datetime,
    snapshot_overrides: Mapping[str, str] | None,
) -> InvocationRecord:
    """Assemble the :class:`InvocationRecord` for one operator action.

    ``snapshot_overrides`` lets story 04a's proxy supply richer values
    for the pipeline-only fields when it has them; unsupplied fields
    fall through to the sentinels. The recognized override keys are the
    seven pipeline-only ``InvocationRecord`` columns; other keys raise
    ``KeyError`` so a typo in 04a fails loud at the boundary instead of
    silently dropping into the sentinel default.
    """
    defaults = {
        "git_sha_at_invocation": _OPERATOR_CONSOLE_SENTINEL,
        "active_profile": _OPERATOR_CONSOLE_SENTINEL,
        "active_regime": _OPERATOR_CONSOLE_SENTINEL,
        "active_overlays_json": _EMPTY_JSON,
        "resolved_config_hash": _OPERATOR_CONSOLE_SENTINEL,
        "resolved_config_snapshot_path": _OPERATOR_CONSOLE_SENTINEL,
        "feature_flags_snapshot_json": _EMPTY_JSON,
        "data_calibration_state_snapshot_path": _OPERATOR_CONSOLE_SENTINEL,
        "data_source_freshness_json": _EMPTY_JSON,
    }
    if snapshot_overrides is not None:
        unknown = set(snapshot_overrides) - set(defaults)
        if unknown:
            msg = (
                f"snapshot_overrides keys must be a subset of {sorted(defaults)!r}; "
                f"got unknown keys {sorted(unknown)!r}"
            )
            raise KeyError(msg)
        defaults.update(snapshot_overrides)

    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=process_lifetime_id,
        start_at=_format_now(now),
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="manual",
        trigger_source=RUN_TYPE_OPERATOR_CONSOLE,
        trigger_reason=f"verb={verb.value} operator_session_id={operator_session_id_}",
        active_mode="normal",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
        **defaults,
    )


@asynccontextmanager
async def operator_invocation(
    *,
    production_session_factory: async_sessionmaker[AsyncSession],
    process_lifetime_id: str,
    operator_session_id_: OperatorSessionId,
    verb: ControlVerb,
    now: datetime | None = None,
    snapshot_overrides: Mapping[str, str] | None = None,
) -> AsyncIterator[InvocationHandle]:
    """Open a short-lived ``InvocationHandle`` for one operator action.

    Three-transaction sequencing (mirroring
    :class:`alphamind.state.invocation_context.context.InvocationContext`):

    1. Mint an invocation_id, assemble an :class:`InvocationRecord`, and
       commit the row in its own short transaction. The row is durable
       on return — subsequent activity-log writes can FK into it from
       any session.
    2. Open a fresh phase session and yield an :class:`InvocationHandle`
       bound to it. The caller's body uses the handle to emit
       activity-log entries scoped to this operator action.
    3. On clean exit, commit the phase session and close it. On
       exception, roll back the phase session's writes only — the
       invocation row remains so the activity-log explorer can still
       surface the attempted operator action.

    Parameters
    ----------
    production_session_factory:
        The **production** :data:`alphamind.persistence.models.Base`-backed
        async session factory (built by
        :func:`alphamind.persistence.session.engine_pair_context` in the
        composition root). This is NOT the command-center writer
        factory — the cc writer rejects any write whose mapped table is
        not one of the three command-center-owned tables, and
        :class:`InvocationRow` lives on the production ``Base``, not on
        :class:`~alphamind.command_center.persistence.tables.CommandCenterBase`.
        ``operator_invocation`` is a legitimate cross-package write (the
        bridge that records an operator action as an invocation row) and
        therefore uses the production factory directly. The cc writer's
        discipline ("command-center views can't accidentally write to OMS
        state") is preserved — this helper's writes go through the
        production factory, not the cc writer.
    process_lifetime_id:
        FK target on the ``invocations`` row. The command center's
        ``ProcessSession`` (story 02's ``session.py``, added in a later
        commit) carries this value.
    operator_session_id_:
        The active operator session — committed to ``trigger_reason``
        for traceability. (Trailing underscore so the parameter name
        doesn't collide with the :func:`alphamind.command_center._kernel.ids.operator_session_id`
        constructor.)
    verb:
        The control verb being dispatched. Committed to ``trigger_reason``
        so the activity-log explorer can filter operator actions per verb.
    now:
        UTC ``datetime`` to stamp on the row + use in the minted
        invocation_id. Defaults to :func:`datetime.now` so production
        callers don't have to thread the clock; tests pass a fixed
        value when needed.
    snapshot_overrides:
        Optional mapping for the pipeline-only fields on the invocation
        row (``git_sha_at_invocation``, ``active_profile``, etc.). Story
        04a's proxy may supply richer values when it has them (e.g. the
        most recent pipeline invocation's ``resolved_config_hash``); all
        unsupplied fields fall through to documented sentinels.

    Yields
    ------
    InvocationHandle
        Standard handle from :mod:`alphamind.state.invocation_context`.
        Use the handle's ``session`` to write activity-log entries
        scoped to this operator action; use ``invocation_id`` as the
        FK target on those entries.
    """
    stamp = now if now is not None else datetime.now(UTC)
    invocation_id = _mint_invocation_id(stamp)
    record = _build_record(
        invocation_id=invocation_id,
        process_lifetime_id=process_lifetime_id,
        operator_session_id_=operator_session_id_,
        verb=verb,
        now=stamp,
        snapshot_overrides=snapshot_overrides,
    )
    await insert_invocation_row(production_session_factory, record)

    session = production_session_factory()
    handle = InvocationHandle(session=session, invocation_id=invocation_id)
    try:
        yield handle
    except BaseException:
        # Wrap the rollback so a failure here (or the rollback itself
        # raising during ``CancelledError`` delivery) does NOT mask the
        # originating exception. Log the rollback failure and re-raise
        # the original — without this, a CancelledError delivered into
        # the body of the ``async with`` would be replaced by the
        # rollback's exception, breaking task cancellation contracts (F5).
        try:
            await session.rollback()
        except Exception as rollback_exc:
            log.warning(
                "operator_invocation rollback during exception/cancel "
                "failed; original exception preserved: %s",
                rollback_exc,
            )
        raise
    else:
        await session.commit()
    finally:
        await session.close()
