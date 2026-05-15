"""Round-trip codec between ``FillRecord`` and ``FillRecordRow`` (story 05 / ALP-363).

The typed Pydantic ``FillRecord`` is the authoritative shape; the SQL row
mirrors it column-for-column. ``record_to_row`` projects a record for INSERT;
``row_to_record`` rehydrates a row back into the typed model for the read
path.

Three sub-shapes serialize as JSON in their own columns: the optional Reg T
attribution, the optional paper-mode live-execution estimate. Datetimes
serialize via ``isoformat`` (matches the convention the orders codec uses).
"""

from __future__ import annotations

import json
from datetime import datetime

from alphamind._kernel.money import money, price, signed_money
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.portfolio_state.records.positions import LiveExecutionEstimate
from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
    RegTMarginAttribution,
)
from alphamind.state.tables.fill_records import FillRecordRow


def record_to_row(record: FillRecord) -> FillRecordRow:
    """Project a ``FillRecord`` to its ``FillRecordRow`` form."""
    return FillRecordRow(
        fill_id=record.fill_id,
        order_id=record.order_id,
        fill_timestamp=record.fill_timestamp.isoformat(),
        fill_price=record.fill_price,
        fill_quantity=record.fill_quantity,
        remaining_quantity_after=record.remaining_quantity_after,
        order_status_after=record.order_status_after.value,
        slippage_usd=record.slippage_usd,
        fees_usd=record.fees_usd,
        execution_venue=record.execution_venue,
        gateway_reference=record.gateway_reference,
        persistence_timestamp=record.persistence_timestamp.isoformat(),
        processing_status=record.processing_status.value,
        processing_invocation_id=record.processing_invocation_id,
        processing_timestamp=(
            record.processing_timestamp.isoformat()
            if record.processing_timestamp is not None
            else None
        ),
        regt_attribution_json=(
            record.regt_attribution.model_dump_json()
            if record.regt_attribution is not None
            else None
        ),
        live_execution_estimate_json=(
            json.dumps(
                {
                    "estimated_spread_usd": record.live_execution_estimate.estimated_spread_usd,
                    "estimated_impact_usd": record.live_execution_estimate.estimated_impact_usd,
                    "estimated_regulatory_fees_usd": (
                        record.live_execution_estimate.estimated_regulatory_fees_usd
                    ),
                    "live_adjusted_fill_price": (
                        record.live_execution_estimate.live_adjusted_fill_price
                    ),
                }
            )
            if record.live_execution_estimate is not None
            else None
        ),
    )


def row_to_record(row: FillRecordRow) -> FillRecord:
    """Rehydrate a ``FillRecordRow`` back into the typed ``FillRecord``."""
    # ALP-462 — the DecimalText column hands back ``Decimal``; rewrap as
    # ``Price`` / ``Money`` (NewType aliases) at the typed-record boundary.
    # ``signed_money`` accommodates negative slippage when the broker reports
    # better-than-quoted fills.
    return FillRecord(
        fill_id=row.fill_id,
        order_id=row.order_id,
        fill_timestamp=datetime.fromisoformat(row.fill_timestamp),
        fill_price=price(row.fill_price),
        fill_quantity=row.fill_quantity,
        remaining_quantity_after=row.remaining_quantity_after,
        order_status_after=OrderStatus(row.order_status_after),
        slippage_usd=signed_money(row.slippage_usd) if row.slippage_usd is not None else None,
        fees_usd=money(row.fees_usd),
        execution_venue=row.execution_venue,
        gateway_reference=row.gateway_reference,
        persistence_timestamp=datetime.fromisoformat(row.persistence_timestamp),
        processing_status=FillProcessingStatus(row.processing_status),
        processing_invocation_id=row.processing_invocation_id,
        processing_timestamp=(
            datetime.fromisoformat(row.processing_timestamp)
            if row.processing_timestamp is not None
            else None
        ),
        regt_attribution=(
            RegTMarginAttribution.model_validate_json(row.regt_attribution_json)
            if row.regt_attribution_json is not None
            else None
        ),
        live_execution_estimate=(
            _live_estimate_from_json(row.live_execution_estimate_json)
            if row.live_execution_estimate_json is not None
            else None
        ),
    )


def _live_estimate_from_json(payload: str) -> LiveExecutionEstimate:
    raw = json.loads(payload)
    return LiveExecutionEstimate(
        estimated_spread_usd=raw["estimated_spread_usd"],
        estimated_impact_usd=raw["estimated_impact_usd"],
        estimated_regulatory_fees_usd=raw["estimated_regulatory_fees_usd"],
        live_adjusted_fill_price=raw["live_adjusted_fill_price"],
    )


__all__ = ["record_to_row", "row_to_record"]
