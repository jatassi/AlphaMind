"""extend activity_log event_type CHECK with BORROW_COST_ACCRUED (ALP-718)

Revision ID: c3b7d5a8e4f2
Revises: d3e7a9c1b502
Create Date: 2026-05-27 17:30:00.000000

ALP-718 added the application-layer ``EventType.BORROW_COST_ACCRUED``
value (the continuous monitor's daily borrow-accrual tick emits one row
per OPEN SHORT EQUITY position with this type). The companion
application-layer + ck_invocations_trigger_source migrations landed, but
the schema-layer ``ck_activity_log_event_type`` CHECK constraint was
last snapshotted by ``a1b2c3d4e5f6_extend_activity_log_profile_switched``
and still rejects the new value. A continuous-monitor process attempting
to write its first ``event_type='BORROW_COST_ACCRUED'`` activity_log row
would trip the constraint and the daily tick would fail in production.

Discovered during ALP-715 review F9 (the parallel ``EventSource``
extension migration's tests exposed the missing event_type vocabulary —
the F9 test had to insert a BORROW_COST_ACCRUED activity_log row to
exercise the new source value, which failed under the prior CHECK).

The migration drops + recreates ``ck_activity_log_event_type`` with the
extended vocabulary. The downgrade reverses the change but refuses to
run if any row already has ``event_type='BORROW_COST_ACCRUED'`` —
otherwise the older constraint would silently forbid values the upgraded
constraint had been admitting, leaving the schema inconsistent.

Mirrors the SQLite-compatibility posture of the prior migrations:
:func:`op.batch_alter_table` is used because SQLite does not support
``ALTER TABLE DROP CONSTRAINT``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3b7d5a8e4f2"
down_revision: str | Sequence[str] | None = "d3e7a9c1b502"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the EventType vocabulary at the time of this migration.
# Mirrors ``a1b2c3d4e5f6``'s ``_EVENT_TYPES_PRE_663`` + ``PROFILE_SWITCHED``.
# Adding new EventType members later requires extending both the application
# enum AND shipping a new migration on top of this one.
_EVENT_TYPES_BEFORE = (
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
    "ENVELOPE_REJECTED",
    "CORPORATE_ACTION_APPLIED",
    "DISTILLATION_CONFIG_CHANGE",
    "RECONCILIATION_ALERT",
    "HALT_ACTIVATED",
    "HALT_LIFTED",
    "GREEKS_REFRESH_FAILED",
    "EMERGENCY_INVOCATION_REQUESTED",
    "RECONCILIATION_CORRECTION",
    "PROFILE_SWITCHED",
)

_NEW_EVENT_TYPE = "BORROW_COST_ACCRUED"


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Replace the event_type CHECK with the vocabulary extended by BORROW_COST_ACCRUED."""
    new_event_types = (*_EVENT_TYPES_BEFORE, _NEW_EVENT_TYPE)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in("event_type", new_event_types),
        )


def downgrade() -> None:
    """Revert to the pre-BORROW_COST_ACCRUED event_type vocabulary.

    Refuses to downgrade when any row already carries the new event type, so
    a routine ``alembic downgrade -1`` cannot silently invalidate accumulated
    activity-log history.
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
            "before re-running the downgrade: "
            f"DELETE FROM activity_log WHERE event_type = '{_NEW_EVENT_TYPE}';"
        )
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in("event_type", _EVENT_TYPES_BEFORE),
        )
