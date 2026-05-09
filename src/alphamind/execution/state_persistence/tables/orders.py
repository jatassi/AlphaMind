"""SQLAlchemy mapping for the ``orders`` table (story 04c / ALP-360).

One row per order — the most write-heavy entity in the OMS. Mirrors the
non-null shape of ``OrderRecord``: ``alpaca_order_id``, ``bracket_id``, and
``submission_timestamp`` are NOT NULL because pre-submission orders are not
representable as ``OrderRecord`` (they live in the activity log / PM-decision
provenance instead, not at the SQL layer).

CHECK constraints on ``order_role`` / ``order_class`` / ``direction`` /
``order_type`` / ``duration`` / ``status`` enforce the same enum vocabularies
the typed record validates, so a future direct-SQL writer faces fail-closed
guarantees. Indexes cover the four hot read paths: status (pending-orders
sweep), position lookup, bracket lookup, Alpaca-side reconciliation.

``position_id`` and ``bracket_id`` are plain TEXT on this mapper; the FKs
to ``positions`` / ``brackets`` are enforced at the schema level by
migration ``e9d2c4f7b3a1_tighten_state_persistence_fks``
(DEFERRABLE INITIALLY DEFERRED, ``ON DELETE RESTRICT``). They are not
declared on this mapper — see ALP-369 for the follow-up that adds
mapper-level declarations.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.orders import (
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRole,
    OrderStatus,
    OrderType,
)

_ORDER_ROLES = tuple(member.value for member in OrderRole)
_ORDER_CLASSES = tuple(member.value for member in OrderClass)
_DIRECTIONS = tuple(member.value for member in OrderDirection)
_ORDER_TYPES = tuple(member.value for member in OrderType)
_DURATIONS = tuple(member.value for member in OrderDuration)
_STATUSES = tuple(member.value for member in OrderStatus)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class OrderRow(Base):
    """Forward-mutable per-order row.

    Tightened-schema fields (NOT NULL, mirroring ``OrderRecord``):
    ``alpaca_order_id``, ``alpaca_order_id_chain_json``, ``bracket_id``,
    ``submission_timestamp``. ``position_id`` and ``average_fill_price``
    remain nullable per the typed record.
    """

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    bracket_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_role: Mapped[str] = mapped_column(Text, nullable=False)
    order_class: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price_parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    duration: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    alpaca_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    alpaca_order_id_chain_json: Mapped[str] = mapped_column(Text, nullable=False)
    submission_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    last_update_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    filled_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    remaining_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    modification_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(_check_in("order_role", _ORDER_ROLES), name="ck_orders_order_role"),
        CheckConstraint(_check_in("order_class", _ORDER_CLASSES), name="ck_orders_order_class"),
        CheckConstraint(_check_in("direction", _DIRECTIONS), name="ck_orders_direction"),
        CheckConstraint(_check_in("order_type", _ORDER_TYPES), name="ck_orders_order_type"),
        CheckConstraint(_check_in("duration", _DURATIONS), name="ck_orders_duration"),
        CheckConstraint(_check_in("status", _STATUSES), name="ck_orders_status"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_position_id", "position_id"),
        Index("ix_orders_bracket_id", "bracket_id"),
        Index("ix_orders_alpaca_order_id", "alpaca_order_id"),
    )
