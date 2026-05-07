"""SQLAlchemy mapping for the ``corporate_action_integration_ledger`` table.

Story 05 / ALP-363. Tracks which Alpaca CA activities the OMS has integrated.
One row per activity, keyed on ``alpaca_activity_id`` (the dedup anchor).
Parallels the fill records' ``processing_status`` mechanism — on Phase 1 retry,
activities already present here are skipped.

Only one terminal status: ``"processed"``. The full activity record is not
persisted locally; ``GET /v2/account/activities`` is the authoritative store
and the activity-log entry written at integration time captures everything
needed for audit. See ``corporate-actions.md`` for the integration spec.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.execution.state_persistence.write_paths.records import (
    CorporateActionLedgerStatus,
)
from alphamind.persistence.models import Base

_PROCESSING_STATUSES = tuple(member.value for member in CorporateActionLedgerStatus)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class CorporateActionIntegrationLedgerRow(Base):
    """Append-only per-CA-activity row."""

    __tablename__ = "corporate_action_integration_ledger"

    alpaca_activity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    processing_invocation_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("invocations.invocation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    processing_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _check_in("processing_status", _PROCESSING_STATUSES),
            name="ck_ca_ledger_processing_status",
        ),
        Index("ix_ca_ledger_processing_invocation_id", "processing_invocation_id"),
        Index("ix_ca_ledger_processing_status", "processing_status"),
    )
