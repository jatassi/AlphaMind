"""SQLAlchemy mapping for the ``counterfactual_replays`` table (ALP-556).

One row per replay attempt produced by the counterfactual replay engine.
Append-only; the engine never silently overwrites. One record per
(envelope, replay_kind) tuple enforced by a UniqueConstraint.

CHECK-constraint vocabularies are the enum ``.value`` tokens from
``alphamind.execution.counterfactual_replay_engine.enums`` — all five
``UnevaluableReason`` members and all five ``ExitLeg`` members are
enumerated from the start so no later story has to retroactively widen a
CHECK on a fresh metadata-built DB.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.persistence.models import Base
from alphamind.state.tables._money_column import DecimalText

# CHECK-constraint vocabularies — frozen from the live enums at table-creation
# time. Listed explicitly (not read lazily) so the DDL is stable across enum
# additions that should *not* widen a deployed table's CHECK without a
# migration.
_REPLAY_KIND_VALUES: tuple[str, ...] = tuple(m.value for m in ReplayKind)
_REPLAY_STATUS_VALUES: tuple[str, ...] = tuple(m.value for m in ReplayStatus)
_UNEVALUABLE_REASON_VALUES: tuple[str, ...] = tuple(m.value for m in UnevaluableReason)
_EXIT_LEG_VALUES: tuple[str, ...] = tuple(m.value for m in ExitLeg)
_CONFIDENCE_VALUES: tuple[str, ...] = tuple(m.value for m in Confidence)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class CounterfactualReplays(Base):
    """Append-only per-replay row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md``
    § Counterfactual replays.
    """

    __tablename__ = "counterfactual_replays"

    replay_id: Mapped[str] = mapped_column(Text, primary_key=True)
    pm_decision_envelope_id: Mapped[str] = mapped_column(Text, nullable=False)
    replay_kind: Mapped[str] = mapped_column(Text, nullable=False)
    replay_status: Mapped[str] = mapped_column(Text, nullable=False)
    unevaluable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    entered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    entry_price: Mapped[str | None] = mapped_column(DecimalText, nullable=True)
    entry_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_slippage: Mapped[str | None] = mapped_column(DecimalText, nullable=True)
    entry_fees: Mapped[str | None] = mapped_column(DecimalText, nullable=True)
    exit_leg: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_price: Mapped[str | None] = mapped_column(DecimalText, nullable=True)
    exit_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_slippage: Mapped[str | None] = mapped_column(DecimalText, nullable=True)
    exit_fees: Mapped[str | None] = mapped_column(DecimalText, nullable=True)
    realized_pl: Mapped[str | None] = mapped_column(DecimalText, nullable=True)
    confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    replay_data_window_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_data_window_end: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_engine_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "pm_decision_envelope_id",
            "replay_kind",
            name="uq_counterfactual_replays_envelope_kind",
        ),
        CheckConstraint(
            _check_in("replay_kind", _REPLAY_KIND_VALUES),
            name="ck_counterfactual_replays_replay_kind",
        ),
        CheckConstraint(
            _check_in("replay_status", _REPLAY_STATUS_VALUES),
            name="ck_counterfactual_replays_replay_status",
        ),
        CheckConstraint(
            f"unevaluable_reason IS NULL OR {_check_in('unevaluable_reason', _UNEVALUABLE_REASON_VALUES)}",
            name="ck_counterfactual_replays_unevaluable_reason",
        ),
        CheckConstraint(
            f"exit_leg IS NULL OR {_check_in('exit_leg', _EXIT_LEG_VALUES)}",
            name="ck_counterfactual_replays_exit_leg",
        ),
        CheckConstraint(
            f"confidence IS NULL OR {_check_in('confidence', _CONFIDENCE_VALUES)}",
            name="ck_counterfactual_replays_confidence",
        ),
        Index("ix_counterfactual_replays_envelope", "pm_decision_envelope_id"),
        Index(
            "ix_counterfactual_replays_status_timestamp",
            "replay_status",
            "replay_timestamp",
        ),
    )


__all__ = ["CounterfactualReplays"]
