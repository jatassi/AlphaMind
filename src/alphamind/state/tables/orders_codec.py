"""Round-trip codec between ``OrderRecord`` and ``OrderRow`` (story 04c / ALP-360).

The typed Pydantic ``OrderRecord`` is the authoritative shape; the SQL row
mirrors its non-null commitments. ``record_to_row`` projects a record to its
row form for INSERT/UPDATE; ``row_to_record`` rehydrates a row back into the
typed model for the read path.

The discriminated-union ``InstrumentSpec`` (equity / options / strategy) and
the Alpaca order-ID chain serialise via ``TypeAdapter`` so every variant /
ordering round-trips through JSON without information loss.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, TypeAdapter

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    CommandId,
    OrderId,
    PositionId,
    ThesisId,
)
from alphamind.portfolio_state.records.orders import (
    InstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
)
from alphamind.state.tables.orders import OrderRow


class _OrderMetadata(BaseModel):
    """Sidecar payload persisted as JSON in ``metadata_json``.

    The bracket-id half of the design-doc Metadata triple lives in
    ``OrderRow.bracket_id`` (a top-level indexed column), so the JSON only
    holds the thesis + PM-command provenance plus ``age_hours`` — a derived
    ``OrderRecord`` field with no counterpart row column.
    """

    model_config = ConfigDict(frozen=True)

    originating_thesis_id: str | None
    originating_pm_command_id: str | None
    age_hours: float


_INSTRUMENT_SPEC_ADAPTER: TypeAdapter[InstrumentSpec] = TypeAdapter(InstrumentSpec)
_ALPACA_CHAIN_ADAPTER: TypeAdapter[tuple[AlpacaOrderId, ...]] = TypeAdapter(
    tuple[AlpacaOrderId, ...]
)


def record_to_row(record: OrderRecord) -> OrderRow:
    """Project an ``OrderRecord`` to its ``OrderRow`` form."""
    metadata = _OrderMetadata(
        originating_thesis_id=record.originating_thesis_id,
        originating_pm_command_id=record.originating_pm_command_id,
        age_hours=record.age_hours,
    )
    return OrderRow(
        order_id=record.order_id,
        position_id=record.position_id,
        bracket_id=record.bracket_id,
        order_role=record.role.value,
        order_class=record.order_class.value,
        instrument_spec_json=_INSTRUMENT_SPEC_ADAPTER.dump_json(record.instrument_spec).decode(),
        direction=record.direction.value,
        order_type=record.order_type.value,
        quantity=record.quantity,
        price_parameters_json=record.price_parameters.model_dump_json(),
        duration=record.duration.value,
        status=record.status.value,
        alpaca_order_id=record.alpaca_order_id,
        alpaca_order_id_chain_json=_ALPACA_CHAIN_ADAPTER.dump_json(
            record.alpaca_order_id_chain
        ).decode(),
        submission_timestamp=record.submission_timestamp.isoformat(),
        last_update_timestamp=record.last_update_timestamp.isoformat(),
        filled_quantity=record.filled_quantity,
        average_fill_price=record.avg_fill_price,
        remaining_quantity=record.remaining_quantity,
        modification_count=record.modification_count,
        metadata_json=metadata.model_dump_json(),
    )


def row_to_record(row: OrderRow) -> OrderRecord:
    """Rehydrate an ``OrderRow`` back into the typed ``OrderRecord``."""
    submission_ts = datetime.fromisoformat(row.submission_timestamp)
    last_update_ts = datetime.fromisoformat(row.last_update_timestamp)
    metadata = _OrderMetadata.model_validate_json(row.metadata_json)
    return OrderRecord(
        order_id=OrderId(row.order_id),
        position_id=PositionId(row.position_id) if row.position_id is not None else None,
        bracket_id=BracketId(row.bracket_id),
        role=OrderRole(row.order_role),
        instrument_spec=_INSTRUMENT_SPEC_ADAPTER.validate_json(row.instrument_spec_json),
        direction=OrderDirection(row.direction),
        order_type=OrderType(row.order_type),
        order_class=OrderClass(row.order_class),
        price_parameters=PriceParameters.model_validate_json(row.price_parameters_json),
        quantity=row.quantity,
        duration=OrderDuration(row.duration),
        status=OrderStatus(row.status),
        alpaca_order_id=AlpacaOrderId(row.alpaca_order_id),
        alpaca_order_id_chain=_ALPACA_CHAIN_ADAPTER.validate_json(row.alpaca_order_id_chain_json),
        submission_timestamp=submission_ts,
        last_update_timestamp=last_update_ts,
        filled_quantity=row.filled_quantity,
        avg_fill_price=row.average_fill_price,
        remaining_quantity=row.remaining_quantity,
        modification_count=row.modification_count,
        originating_thesis_id=ThesisId(metadata.originating_thesis_id)
        if metadata.originating_thesis_id is not None
        else None,
        originating_pm_command_id=CommandId(metadata.originating_pm_command_id)
        if metadata.originating_pm_command_id is not None
        else None,
        age_hours=metadata.age_hours,
    )


__all__ = ["record_to_row", "row_to_record"]
