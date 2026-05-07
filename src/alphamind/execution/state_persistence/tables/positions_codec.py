"""Round-trip codec between ``PositionRecord`` and ``PositionRow``.

The typed Pydantic ``PositionRecord`` is the authoritative shape; the SQL
row mirrors it. ``record_to_row`` projects the record to its row form for
INSERT/UPDATE; ``row_to_record`` rehydrates a row back into the typed model
for the read path. Both are pure functions.

The discriminated-union ``PositionDetailsPayload`` (equity / options /
strategy variants) and the ``tuple[PositionFill, ...]`` execution history
serialise via ``TypeAdapter`` so the round-trip stays faithful — every
optional sub-field, every freshness-metadata element, every greek round-trips
through JSON without information loss.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import TypeAdapter

from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.portfolio_state.records.positions import (
    Direction,
    PositionDetailsPayload,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

# TypeAdapters drive the discriminated-union JSON round-trip. Pydantic's
# discriminator field on ``PositionDetailsPayload`` ensures ``validate_json``
# selects the correct variant from the ``instrument_type`` value.
_DETAILS_ADAPTER: TypeAdapter[PositionDetailsPayload] = TypeAdapter(PositionDetailsPayload)
_HISTORY_ADAPTER: TypeAdapter[tuple[PositionFill, ...]] = TypeAdapter(tuple[PositionFill, ...])


def record_to_row(record: PositionRecord) -> PositionRow:
    """Project a ``PositionRecord`` to its ``PositionRow`` form.

    The Pydantic ``PositionRecord`` validator has already enforced status /
    direction / spinoff invariants by the time this function runs, so the
    projection is straightforward.
    """
    return PositionRow(
        position_id=record.position_id,
        thesis_id=record.thesis_id,
        bracket_id=record.bracket_id,
        status=record.status.value,
        direction=record.direction.value,
        entry_timestamp=(
            record.entry_timestamp.isoformat() if record.entry_timestamp is not None else None
        ),
        instrument_type=record.instrument_type.value,
        details_json=_DETAILS_ADAPTER.dump_json(record.details).decode(),
        execution_history_json=_HISTORY_ADAPTER.dump_json(record.execution_history).decode(),
        realized_pnl_to_date_usd=record.realized_pnl_to_date_usd,
        corporate_action_adjustment_needed=1 if record.corporate_action_adjustment_needed else 0,
        parent_position_id=record.parent_position_id,
        origin=record.origin,
    )


def row_to_record(row: PositionRow) -> PositionRecord:
    """Rehydrate a ``PositionRow`` back into the typed ``PositionRecord``.

    Validates that ``row.instrument_type`` agrees with the discriminator
    embedded in ``row.details_json`` — a mismatch can only arise from a
    hand-crafted INSERT that bypassed the codec, so it is treated as a
    fail-closed read error.
    """
    details = _DETAILS_ADAPTER.validate_json(row.details_json)
    if details.instrument_type.value != row.instrument_type:
        msg = (
            f"row.instrument_type={row.instrument_type!r} disagrees with "
            f"details_json discriminator={details.instrument_type.value!r} "
            f"for position_id={row.position_id!r}"
        )
        raise ValueError(msg)

    return PositionRecord(
        position_id=row.position_id,
        thesis_id=row.thesis_id,
        bracket_id=row.bracket_id,
        status=PositionStatus(row.status),
        direction=Direction(row.direction),
        entry_timestamp=(
            datetime.fromisoformat(row.entry_timestamp) if row.entry_timestamp is not None else None
        ),
        details=details,
        execution_history=_HISTORY_ADAPTER.validate_json(row.execution_history_json),
        realized_pnl_to_date_usd=row.realized_pnl_to_date_usd,
        corporate_action_adjustment_needed=bool(row.corporate_action_adjustment_needed),
        parent_position_id=row.parent_position_id,
        origin=row.origin,
    )


__all__ = ["record_to_row", "row_to_record"]
