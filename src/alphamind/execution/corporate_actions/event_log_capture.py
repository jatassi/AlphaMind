"""Corporate-action → append-only broker-event log capture (ALP-849 / W1c).

A corporate-action is one of the three families of broker-to-local events the
``broker_event_log`` carries (fills + activities + **corporate-actions**,
ADR-0002). Every CA lands as one immutable row through the canonical idempotent
helper :func:`alphamind.execution.write_paths.broker_event_persistence.append_broker_event`
(keyed on the ``event_key`` PK), carrying the resolved position → thesis link on
its INITIAL insert (no read-then-write backfill). Capture is append-only; the
position / cash mutation is the separate per-type integration step (the
handlers). The integration of a CA into the projection / Intent reads the log;
this module only writes it.

``CA_*`` is the event-log vocabulary (``BrokerEventType``), distinct from the
integration vocabulary (``CorporateActionType``) — the two are mapped here. The
v1beta1 types the fetcher used to drop (``WorthlessRemoval`` / ``UnitSplit`` /
``Redemption``) are captured too: they carry no position-mutation math, so they
are capture-only (no integration handler), but the broker fact still lands on
the gap-free log.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from alphamind._kernel.ids import PositionId, ThesisId
from alphamind.execution.write_paths.broker_event_persistence import append_broker_event
from alphamind.portfolio_state.events.activity_log import CorporateActionType
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
    serialize_event_payload,
)
from alphamind.state.tables.positions import PositionRow

from .types import CorporateActionActivity

# Integration vocabulary (``CorporateActionType``) → event-log vocabulary
# (``BrokerEventType``). Cash-dividend LONG / SHORT collapse to one CA event type
# (the long/short split is an integration-side accounting concern, not a distinct
# broker fact); both merger shapes collapse to ``CA_MERGER``.
_CA_TYPE_TO_EVENT_TYPE: dict[CorporateActionType, BrokerEventType] = {
    CorporateActionType.SPLIT: BrokerEventType.CA_SPLIT,
    CorporateActionType.REVERSE_SPLIT: BrokerEventType.CA_REVERSE_SPLIT,
    CorporateActionType.STOCK_DIVIDEND: BrokerEventType.CA_STOCK_DIVIDEND,
    CorporateActionType.CASH_DIVIDEND_LONG: BrokerEventType.CA_CASH_DIVIDEND,
    CorporateActionType.CASH_DIVIDEND_SHORT: BrokerEventType.CA_CASH_DIVIDEND,
    CorporateActionType.CASH_MERGER: BrokerEventType.CA_MERGER,
    CorporateActionType.STOCK_MERGER: BrokerEventType.CA_MERGER,
    CorporateActionType.SPIN_OFF: BrokerEventType.CA_SPINOFF,
    CorporateActionType.SYMBOL_CHANGE: BrokerEventType.CA_SYMBOL_CHANGE,
    # Capture-only v1beta1 types (no integration handler) — gap-free fact only.
    CorporateActionType.WORTHLESS_REMOVAL: BrokerEventType.CA_WORTHLESS_REMOVAL,
    CorporateActionType.UNIT_SPLIT: BrokerEventType.CA_UNIT_SPLIT,
    CorporateActionType.REDEMPTION: BrokerEventType.CA_REDEMPTION,
}


def ca_event_key(alpaca_activity_id: str) -> str:
    """Stable ``broker_event_log`` idempotency key for a corporate-action.

    The Alpaca activity id is broker-unique, so ``ca:{id}`` is the natural
    ``event_key`` — a fill collection retry or a late broker re-post of the same activity
    collapses onto the one row (the idempotency guarantee ADR-0002's gap-free log
    relies on).
    """
    return f"ca:{alpaca_activity_id}"


async def capture_ca_event(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
) -> bool:
    """Append one ``broker_event_log`` row for *activity*; return newness.

    Resolves the position → thesis Intent edge (ADR-0002) BEFORE the append so
    the row carries the resolved ``thesis_id`` / ``position_id`` on its INITIAL
    insert. ``invocation_id`` is ``NULL`` — a CA carries no broker-carried
    ``client_order_id`` and so no originating invocation; it attributes via the
    position → thesis edge. Idempotent on the ``event_key`` PK
    (``ON CONFLICT DO NOTHING``); returns ``True`` iff this call newly inserted
    the row.

    ``position_id`` / ``thesis_id`` are set only when the local position row
    actually exists (both FKs ``ON DELETE RESTRICT``): a CA referencing a missing
    position — which the per-type handler then surfaces as a ``ValueError`` —
    appends a NULL link rather than violating the FK, and the surrounding
    transaction rolls back wholesale when the handler raises.
    """
    position_id, thesis_id = await _resolve_position_link(handle, activity.position_id)
    record = BrokerEventRecord(
        event_key=ca_event_key(activity.alpaca_activity_id),
        event_type=_CA_TYPE_TO_EVENT_TYPE[activity.action_type],
        thesis_id=ThesisId(thesis_id) if thesis_id is not None else None,
        invocation_id=None,
        position_id=PositionId(position_id) if position_id is not None else None,
        raw_payload_json=serialize_event_payload(activity.model_dump(mode="json")),
        broker_timestamp=activity.transaction_time,
        captured_at=datetime.now(UTC),
    )
    return await append_broker_event(handle.session, record)


async def _resolve_position_link(
    handle: InvocationHandle, position_id: str
) -> tuple[str | None, str | None]:
    """Resolve ``(position_id, thesis_id)`` off the local position row.

    Returns ``(None, None)`` when the position row does not exist — the CA's link
    is not yet representable as a valid FK, and the per-type handler raises on the
    missing position so the transaction never commits the NULL-link row.
    """
    stmt = select(PositionRow.position_id, PositionRow.thesis_id).where(
        PositionRow.position_id == position_id
    )
    row = (await handle.session.execute(stmt)).one_or_none()
    if row is None:
        return None, None
    return row[0], row[1]


__all__ = ["ca_event_key", "capture_ca_event"]
