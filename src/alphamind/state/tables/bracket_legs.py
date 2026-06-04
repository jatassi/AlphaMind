"""SQLAlchemy mapping for the ``bracket_legs`` table (story 04d / ALP-361).

One row per protective leg. Each row carries a discriminated trigger
payload (``PriceTrigger`` / ``TimeTrigger`` / ``EventTrigger``) JSON-
serialized via ``model_dump_json()`` plus an optional ``PLAnchorSpec``
JSON for P/L-anchored legs.

FK on ``bracket_id`` with ``ON DELETE RESTRICT`` — bracket rows are
append-only, and a parent bracket cannot be deleted while children
reference it. ``order_id`` is nullable for ``EVENT_INVALIDATION`` legs and
carries a DEFERRABLE INITIALLY DEFERRED FK to ``orders.order_id``
(``ON DELETE RESTRICT``), matching the
``e9d2c4f7b3a1_tighten_state_persistence_fks`` migration. The deferral
accommodates Phase 2's OPEN writeback, which inserts the protective leg
order and the parent bracket in the same transaction. The UNIQUE constraint
on ``(bracket_id, leg_index)`` preserves leg-ordering invariants.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.orders import (
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    EnforcementBinding,
)

_LEG_TYPES = tuple(member.value for member in BracketLegType)
_TRIGGER_KINDS = ("PRICE", "TIME", "EVENT")
_ENFORCEMENTS = tuple(member.value for member in BracketLegEnforcement)
_ENFORCEMENT_BINDINGS = tuple(member.value for member in EnforcementBinding)
_LEG_STATUSES = tuple(member.value for member in BracketLegStatus)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class BracketLegRow(Base):
    """Forward-only per-leg row within a bracket's protective set.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Brackets.
    CHECK constraints on ``leg_type`` / ``trigger_kind`` / ``enforcement``
    / ``leg_status`` encode the same vocabularies the typed
    ``BracketLeg`` enforces, so a future direct-SQL writer faces the
    same fail-closed guarantees the application path enforces.
    """

    __tablename__ = "bracket_legs"

    bracket_leg_id: Mapped[str] = mapped_column(Text, primary_key=True)
    bracket_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("brackets.bracket_id", ondelete="RESTRICT"),
        nullable=False,
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    leg_type: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "orders.order_id",
            name="fk_bracket_legs_order_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    trigger_kind: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    pl_anchor_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    enforcement: Mapped[str] = mapped_column(Text, nullable=False)
    # ADR-0003: the typed broker-vs-monitor enforcement binding. CHECK covers
    # both members so a future direct-SQL writer faces the same fail-closed
    # guarantee the typed record enforces.
    enforcement_binding: Mapped[str] = mapped_column(Text, nullable=False)
    leg_status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _check_in("leg_type", _LEG_TYPES),
            name="ck_bracket_legs_leg_type",
        ),
        CheckConstraint(
            _check_in("trigger_kind", _TRIGGER_KINDS),
            name="ck_bracket_legs_trigger_kind",
        ),
        CheckConstraint(
            _check_in("enforcement", _ENFORCEMENTS),
            name="ck_bracket_legs_enforcement",
        ),
        CheckConstraint(
            _check_in("enforcement_binding", _ENFORCEMENT_BINDINGS),
            name="ck_bracket_legs_enforcement_binding",
        ),
        CheckConstraint(
            _check_in("leg_status", _LEG_STATUSES),
            name="ck_bracket_legs_leg_status",
        ),
        Index("ix_bracket_legs_bracket_id", "bracket_id"),
        Index(
            "uq_bracket_legs_bracket_id_leg_index",
            "bracket_id",
            "leg_index",
            unique=True,
        ),
    )
