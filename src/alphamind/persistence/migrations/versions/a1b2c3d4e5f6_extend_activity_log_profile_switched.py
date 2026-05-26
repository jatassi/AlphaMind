"""extend activity_log event_type CHECK with PROFILE_SWITCHED

Revision ID: a1b2c3d4e5f6
Revises: e2f7a1c3b8d9
Create Date: 2026-05-26 00:00:00.000000

ALP-663 ships the substrate for the operator-console profile-switch handler
(story 04a). When the operator calls ``POST /api/control/switch_profile`` and
the switch is not a no-op, the handler emits a ``PROFILE_SWITCHED`` row in the
``CONFIGURATION`` event group.

The CHECK constraint on ``event_type`` must be extended so the new vocabulary
survives schema validation on the production DB (created under an earlier head
migration). The new type reuses the ``CONFIGURATION`` event group — no
``event_group`` extension needed.

SQLite does not support direct CHECK-constraint alteration, so the upgrade
recreates the constraint via :func:`op.batch_alter_table`. The downgrade
refuses to run if any rows already carry the new event type to avoid silently
invalidating accumulated operator-audit history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "e2f7a1c3b8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the EventType vocabulary at the time of forward migration. If
# you add EventType members in subsequent migrations and then run
# ``alembic downgrade -1`` past this one, the CHECK constraint will narrow
# to this snapshot — re-extend the constraint in your forward migration to
# avoid silently invalidating future writes.
_EVENT_TYPES_PRE_663 = (
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
)

_NEW_EVENT_TYPE = "PROFILE_SWITCHED"


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Replace the event_type CHECK with the vocab extended by the new value.

    The new type reuses ``CONFIGURATION`` — no event_group change.
    """
    new_event_types = (*_EVENT_TYPES_PRE_663, _NEW_EVENT_TYPE)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in("event_type", new_event_types),
        )


def downgrade() -> None:
    """Revert to the pre-ALP-663 event_type vocabulary.

    Refuses to downgrade when any row already carries the new event type, so
    a routine ``alembic downgrade -1`` cannot silently invalidate accumulated
    operator-audit history.
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
            _check_in("event_type", _EVENT_TYPES_PRE_663),
        )
