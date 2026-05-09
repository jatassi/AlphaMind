"""Round-trip codec between ``BracketRecord`` and ``brackets`` /
``bracket_legs`` SQL rows (story 04d / ALP-361).

The typed records are the source of truth; this module is the only place
that knows the row shape. Callers operate on the records and let the
codec materialize / hydrate rows.

The discriminated trigger union (``PriceTrigger | TimeTrigger | EventTrigger``)
and the optional ``PLAnchorSpec`` round-trip via Pydantic ``TypeAdapter`` so
the JSON payloads remain validated against the same vocabularies the typed
records enforce.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import TypeAdapter

from alphamind.execution.state_persistence.tables.bracket_legs import BracketLegRow
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegModification,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PLAnchorSpec,
    TriggerPayload,
)

_TRIGGER_ADAPTER: TypeAdapter[TriggerPayload] = TypeAdapter(TriggerPayload)
_PL_ANCHOR_ADAPTER: TypeAdapter[PLAnchorSpec] = TypeAdapter(PLAnchorSpec)
_MODIFICATION_HISTORY_ADAPTER: TypeAdapter[tuple[BracketLegModification, ...]] = TypeAdapter(
    tuple[BracketLegModification, ...]
)


def record_to_rows(record: BracketRecord) -> tuple[BracketRow, tuple[BracketLegRow, ...]]:
    """Decompose a typed ``BracketRecord`` into its parent + leg rows.

    Leg ordering is preserved via ``leg_index`` matching the position
    within ``record.protective_legs``.
    """
    bracket_row = BracketRow(
        bracket_id=record.bracket_id,
        position_id=record.position_id,
        status=record.status.value,
        entry_order_id=record.entry_order_id,
        entry_window_deadline=(
            record.entry_window_deadline.isoformat()
            if record.entry_window_deadline is not None
            else None
        ),
        corporate_action_cancellation_reason=record.corporate_action_cancellation_reason,
        modification_history_json=_MODIFICATION_HISTORY_ADAPTER.dump_json(
            record.modification_history
        ).decode(),
    )
    leg_rows = tuple(
        _leg_to_row(leg, bracket_id=record.bracket_id, leg_index=idx)
        for idx, leg in enumerate(record.protective_legs)
    )
    return bracket_row, leg_rows


def rows_to_record(bracket_row: BracketRow, leg_rows: tuple[BracketLegRow, ...]) -> BracketRecord:
    """Reconstruct a typed ``BracketRecord`` from its parent + leg rows.

    ``leg_rows`` must arrive sorted by ``leg_index`` ascending; the codec
    does not re-sort because callers normally read with an
    ``ORDER BY leg_index`` clause.
    """
    legs = tuple(_row_to_leg(row) for row in leg_rows)
    history = _MODIFICATION_HISTORY_ADAPTER.validate_json(bracket_row.modification_history_json)
    return BracketRecord(
        bracket_id=bracket_row.bracket_id,
        position_id=bracket_row.position_id,
        status=BracketStatus(bracket_row.status),
        entry_order_id=bracket_row.entry_order_id,
        protective_legs=legs,
        modification_history=history,
        corporate_action_cancellation_reason=bracket_row.corporate_action_cancellation_reason,
        entry_window_deadline=_parse_optional_datetime(bracket_row.entry_window_deadline),
    )


def _leg_to_row(leg: BracketLeg, *, bracket_id: str, leg_index: int) -> BracketLegRow:
    return BracketLegRow(
        bracket_leg_id=leg.leg_id,
        bracket_id=bracket_id,
        leg_index=leg_index,
        leg_type=leg.leg_type.value,
        order_id=leg.order_id,
        trigger_kind=leg.trigger.trigger_type.upper(),
        trigger_payload_json=_TRIGGER_ADAPTER.dump_json(leg.trigger).decode(),
        pl_anchor_json=(
            _PL_ANCHOR_ADAPTER.dump_json(leg.pl_anchor).decode()
            if leg.pl_anchor is not None
            else None
        ),
        enforcement=leg.enforcement.value,
        leg_status=leg.status.value,
    )


def _row_to_leg(row: BracketLegRow) -> BracketLeg:
    pl_anchor = (
        _PL_ANCHOR_ADAPTER.validate_json(row.pl_anchor_json)
        if row.pl_anchor_json is not None
        else None
    )
    return BracketLeg(
        leg_id=row.bracket_leg_id,
        leg_type=BracketLegType(row.leg_type),
        order_id=row.order_id,
        trigger=_TRIGGER_ADAPTER.validate_json(row.trigger_payload_json),
        enforcement=BracketLegEnforcement(row.enforcement),
        status=BracketLegStatus(row.leg_status),
        pl_anchor=pl_anchor,
    )


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
