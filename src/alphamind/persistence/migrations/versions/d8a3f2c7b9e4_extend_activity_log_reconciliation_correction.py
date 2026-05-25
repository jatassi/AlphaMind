"""extend activity_log event_type CHECK with RECONCILIATION_CORRECTION

Revision ID: d8a3f2c7b9e4
Revises: d3f6a1c7e9b2
Create Date: 2026-05-25 00:00:00.000000

ALP-619 wires post-Phase-1 reconciliation drift back to local state. When
``reconcile()`` detects drift on an existing OPEN equity/options position or
the singleton cash row, it now writes Alpaca's value back AND emits a
``RECONCILIATION_CORRECTION`` row alongside the existing
``RECONCILIATION_ALERT`` — the operator gets both the alert (forensic trail
preserved) and the audit row capturing the prior local value and the applied
Alpaca value.

Alpaca-only orphans (a symbol present in ``GET /v2/positions`` with no
matching local row) continue to alert-only — synthesizing a local position
record requires a thesis_id, a cost basis, and an execution history we can't
honestly derive from the Alpaca snapshot.

The CHECK constraint on ``event_type`` must be extended so the new vocabulary
survives schema validation on the production DB (created under an earlier
head migration). The new type reuses the ``RECONCILIATION`` event group — no
``event_group`` extension needed.

SQLite does not support direct CHECK-constraint alteration, so the upgrade
recreates the constraint via :func:`op.batch_alter_table`. The downgrade
refuses to run if any rows already carry the new event type to avoid silently
invalidating accumulated reconciliation history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8a3f2c7b9e4"
down_revision: str | Sequence[str] | None = "d3f6a1c7e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the EventType vocabulary at the time of forward migration. If
# you add EventType members in subsequent migrations and then run
# ``alembic downgrade -1`` past this one, the CHECK constraint will narrow
# to this snapshot — re-extend the constraint in your forward migration to
# avoid silently invalidating future writes.
_EVENT_TYPES_PRE_619 = (
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
)

_NEW_EVENT_TYPE = "RECONCILIATION_CORRECTION"


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Replace the event_type CHECK with the vocab extended by the new value.

    The new type reuses ``RECONCILIATION`` — no event_group change.
    """
    new_event_types = (*_EVENT_TYPES_PRE_619, _NEW_EVENT_TYPE)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in("event_type", new_event_types),
        )


def downgrade() -> None:
    """Revert to the pre-ALP-619 event_type vocabulary.

    Refuses to downgrade when any row already carries the new event type, so
    a routine ``alembic downgrade -1`` cannot silently invalidate accumulated
    reconciliation history.
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
            _check_in("event_type", _EVENT_TYPES_PRE_619),
        )
