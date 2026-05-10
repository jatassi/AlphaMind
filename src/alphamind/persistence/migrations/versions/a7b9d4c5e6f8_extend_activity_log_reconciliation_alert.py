"""extend activity_log CHECKs with reconciliation_alert + RECONCILIATION group

Revision ID: a7b9d4c5e6f8
Revises: f3a8e2c5b6d4
Create Date: 2026-05-10 00:00:00.000000

ALP-415 (story 04) adds the ``RECONCILIATION_ALERT`` event type and the new
``RECONCILIATION`` event group to the activity_log catalog. The Phase 1 CA
pipeline's post-merge reconciliation step emits one alert per unexplained
delta between local state and Alpaca's authoritative ``GET /v2/positions`` /
``GET /v2/account`` snapshot — see ``corporate-actions.md § Phase 1
integration sequence`` step 4.

Both the ``event_type`` and ``event_group`` CHECK constraints must be extended
so the new vocabulary survives schema validation. SQLite does not support
direct CHECK-constraint alteration, so the upgrade recreates both constraints
via :func:`op.batch_alter_table`. The downgrade refuses to run if any rows
already carry the new event type to avoid silently invalidating accumulated
reconciliation history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b9d4c5e6f8"
down_revision: str | Sequence[str] | None = "f3a8e2c5b6d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the EventType vocabulary at the time of forward migration. If
# you add EventType members in subsequent migrations and then run
# ``alembic downgrade -1`` past this one, the CHECK constraint will narrow to
# this snapshot — re-extend the constraint in your forward migration to avoid
# silently invalidating future writes.
_EVENT_TYPES_PRE_415 = (
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
)

_EVENT_GROUPS_PRE_415 = (
    "POSITION_LIFECYCLE",
    "ORDER_LIFECYCLE",
    "BRACKET",
    "THESIS",
    "CASH_AND_MARGIN",
    "RISK_AND_GUARDRAIL",
    "PM_DECISION",
    "CORPORATE_ACTION",
    "CONFIGURATION",
)

_NEW_EVENT_TYPE = "RECONCILIATION_ALERT"
_NEW_EVENT_GROUP = "RECONCILIATION"


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Replace both CHECK constraints with vocab extended by the new value."""
    new_event_types = (*_EVENT_TYPES_PRE_415, _NEW_EVENT_TYPE)
    new_event_groups = (*_EVENT_GROUPS_PRE_415, _NEW_EVENT_GROUP)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in("event_type", new_event_types),
        )
        batch_op.drop_constraint("ck_activity_log_event_group", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_group",
            _check_in("event_group", new_event_groups),
        )


def downgrade() -> None:
    """Revert to the pre-ALP-415 event_type / event_group vocabularies.

    Refuses to downgrade when any row already carries the new event type,
    so a routine ``alembic downgrade -1`` cannot silently invalidate
    accumulated reconciliation history.
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
            _check_in("event_type", _EVENT_TYPES_PRE_415),
        )
        batch_op.drop_constraint("ck_activity_log_event_group", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_group",
            _check_in("event_group", _EVENT_GROUPS_PRE_415),
        )
