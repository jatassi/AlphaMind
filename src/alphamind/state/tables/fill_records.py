"""SQLAlchemy mapping for the ``fill_records`` table (story 05 / ALP-363).

One row per fill event with ``processing_status`` flag (unprocessed /
processed / quarantined). The continuous monitor appends new fills as
``unprocessed``; fill collection marks them ``processed`` inside the same atomic
transaction that mutates positions / orders / cash.

Two FKs with ``ON DELETE RESTRICT``:

* ``order_id`` → ``orders.order_id`` — the raw execution history must remain
  joinable to the parent order.
* ``processing_invocation_id`` → ``invocations.invocation_id`` — null until
  fill collection marks the fill processed; nullable on the SQL side, application-level
  invariant enforces the lifecycle (see ``write_paths.records.FillRecord``).

Deduplication: composite UNIQUE on ``(order_id, fill_timestamp, fill_quantity,
fill_price)``. On retry (transient storage failure, ``GET /v2/orders`` replay),
``append_fill_record`` silently skips duplicates per the design-doc contract.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.state.records import (
    FillProcessingStatus,
)
from alphamind.state.tables._money_column import DecimalText

_ORDER_STATUSES = tuple(member.value for member in OrderStatus)
_PROCESSING_STATUSES = tuple(member.value for member in FillProcessingStatus)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class FillRecordRow(Base):
    """Append-only per-fill row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Fill records.
    """

    __tablename__ = "fill_records"

    fill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("orders.order_id", ondelete="RESTRICT"),
        nullable=False,
    )
    fill_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    fill_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    remaining_quantity_after: Mapped[float] = mapped_column(Float, nullable=False)
    order_status_after: Mapped[str] = mapped_column(Text, nullable=False)
    slippage_usd: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    fees_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    execution_venue: Mapped[str | None] = mapped_column(Text, nullable=True)
    gateway_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    persistence_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False)
    processing_invocation_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("invocations.invocation_id", ondelete="RESTRICT"),
        nullable=True,
    )
    processing_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    regt_attribution_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    live_execution_estimate_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "fill_timestamp",
            "fill_quantity",
            "fill_price",
            name="uq_fill_records_dedupe",
        ),
        CheckConstraint(
            _check_in("order_status_after", _ORDER_STATUSES),
            name="ck_fill_records_order_status_after",
        ),
        CheckConstraint(
            _check_in("processing_status", _PROCESSING_STATUSES),
            name="ck_fill_records_processing_status",
        ),
        Index("ix_fill_records_processing_status", "processing_status"),
        Index("ix_fill_records_order_id", "order_id"),
    )
