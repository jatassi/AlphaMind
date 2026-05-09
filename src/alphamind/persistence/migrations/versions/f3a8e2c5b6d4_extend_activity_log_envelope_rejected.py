"""extend activity_log.event_type CHECK with envelope_rejected

Revision ID: f3a8e2c5b6d4
Revises: e9d2c4f7b3a1
Create Date: 2026-05-09 00:00:00.000000

ALP-368 adds the ``ENVELOPE_REJECTED`` event type for Layer-2/3 envelope-level
guardrail rejections (invariant violations, cross-command coherence failures
from ``validate_pm_envelope``). Mirrors d4a8e9f2c1b3's pattern for
``ENVELOPE_PARSE_FAILED`` (Layer-1 parse failures); both event types are
PM_DECISION-grouped envelope-level forensics.

SQLite does not support direct CHECK-constraint alteration, so the upgrade
recreates the constraint via :func:`op.batch_alter_table`. The downgrade
refuses to run if any rows already carry the new event type to avoid silently
invalidating accumulated audit history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a8e2c5b6d4"
down_revision: str | Sequence[str] | None = "e9d2c4f7b3a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the EventType vocabulary at the time of forward migration. If
# you add EventType members in subsequent migrations and then run
# ``alembic downgrade -1`` past this one, the CHECK constraint will narrow to
# this snapshot — re-extend the constraint in your forward migration to avoid
# silently invalidating future writes.
_EVENT_TYPES_PRE_368 = (
    "POSITION_OPENED",
    "POSITION_CLOSED",
    "POSITION_ADDED",
    "POSITION_REDUCED",
    "ORDER_SUBMITTED",
    "ORDER_FILLED",
    "ORDER_PARTIALLY_FILLED",
    "ORDER_CANCELLED",
    "ORDER_EXPIRED",
    "ORDER_REJECTED",
    "ORDER_MODIFIED",
    "BRACKET_ACTIVATED",
    "BRACKET_COMPLETED",
    "BRACKET_DISSOLVED",
    "BRACKET_MODIFIED",
    "BRACKET_INCOMPLETE_WARNING",
    "BRACKET_CANCELLED_CORPORATE_ACTION",
    "THESIS_CREATED",
    "THESIS_COMPONENT_ADDED",
    "THESIS_COMPONENT_UPDATED",
    "THESIS_RESOLVED",
    "THESIS_STATUS_CHANGED",
    "CASH_DEBITED",
    "CASH_CREDITED",
    "CAPITAL_RESERVED",
    "CAPITAL_RELEASED",
    "MARGIN_CALL",
    "MARGIN_CALL_RESOLVED",
    "MARGIN_LIQUIDATION",
    "GUARDRAIL_REJECTION",
    "RISK_LIMIT_APPROACHED",
    "RISK_PARAMETER_CHANGED",
    "PM_DECISION",
    "COMMAND_ABANDONED",
    "ENVELOPE_PARSE_FAILED",
    "CORPORATE_ACTION_APPLIED",
    "DISTILLATION_CONFIG_CHANGE",
)

_NEW_EVENT_TYPE = "ENVELOPE_REJECTED"


def _check_in(values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"event_type IN ({rendered})"


def upgrade() -> None:
    """Replace the event_type CHECK constraint with one that accepts the new value."""
    new_vocab = (*_EVENT_TYPES_PRE_368, _NEW_EVENT_TYPE)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in(new_vocab),
        )


def downgrade() -> None:
    """Revert to the pre-ALP-368 event_type vocabulary.

    Refuses to downgrade when any row already carries the new event type,
    so a routine ``alembic downgrade -1`` cannot silently invalidate
    accumulated envelope-rejection audit history.
    """
    bind = op.get_bind()
    surviving = bind.execute(
        sa.text("SELECT COUNT(*) FROM activity_log WHERE event_type = :et"),
        {"et": _NEW_EVENT_TYPE},
    ).scalar_one()
    if surviving:
        raise RuntimeError(
            f"refusing to downgrade revision {revision}: "
            f"{surviving} '{_NEW_EVENT_TYPE}' row(s) in activity_log would be "
            "invalidated by the older CHECK constraint. Clear them manually "
            f"before re-running the downgrade: "
            f"DELETE FROM activity_log WHERE event_type = '{_NEW_EVENT_TYPE}';"
        )
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in(_EVENT_TYPES_PRE_368),
        )
