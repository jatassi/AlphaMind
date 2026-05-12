"""extend activity_log event_type CHECK with the four continuous-monitor event types

Revision ID: b2c4f7a3d9e8
Revises: a7b9d4c5e6f8
Create Date: 2026-05-11 00:00:00.000000

ALP-123 (continuous monitor) adds four new ``EventType`` members in the
``RISK_AND_GUARDRAIL`` group:

* ``HALT_ACTIVATED`` / ``HALT_LIFTED`` — emitted by the breach-evaluation
  loop's :class:`HaltTransitionTracker` on inactive→active / active→inactive
  daily-halt and cumulative-tier-3 full-halt transitions.
* ``GREEKS_REFRESH_FAILED`` — emitted by the greeks-refresh task when the
  IV-fetch retry budget is exhausted for an open option/strategy position.
* ``EMERGENCY_INVOCATION_REQUESTED`` — emitted by the emergency-invocation
  trigger evaluator when one of the four trigger criteria fires.

The CHECK constraint on ``event_type`` must be extended so the new
vocabulary survives schema validation on the production DB (created under
an earlier head migration). The four types reuse ``RISK_AND_GUARDRAIL`` —
no ``event_group`` extension needed.

SQLite does not support direct CHECK-constraint alteration, so the upgrade
recreates the constraint via :func:`op.batch_alter_table`. The downgrade
refuses to run if any rows already carry one of the new event types to
avoid silently invalidating accumulated monitor history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c4f7a3d9e8"
down_revision: str | Sequence[str] | None = "a7b9d4c5e6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the EventType vocabulary at the time of forward migration. If
# you add EventType members in subsequent migrations and then run
# ``alembic downgrade -1`` past this one, the CHECK constraint will narrow
# to this snapshot — re-extend the constraint in your forward migration to
# avoid silently invalidating future writes.
_EVENT_TYPES_PRE_123 = (
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
)

_NEW_EVENT_TYPES = (
    "HALT_ACTIVATED",
    "HALT_LIFTED",
    "GREEKS_REFRESH_FAILED",
    "EMERGENCY_INVOCATION_REQUESTED",
)


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Replace the event_type CHECK with the vocab extended by the four new values.

    The new types reuse ``RISK_AND_GUARDRAIL`` — no event_group change.
    """
    new_event_types = (*_EVENT_TYPES_PRE_123, *_NEW_EVENT_TYPES)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in("event_type", new_event_types),
        )


def downgrade() -> None:
    """Revert to the pre-ALP-123 event_type vocabulary.

    Refuses to downgrade when any row carries one of the new event types,
    so a routine ``alembic downgrade -1`` cannot silently invalidate
    accumulated monitor history.
    """
    bind = op.get_bind()
    placeholders = ", ".join(f":et{i}" for i in range(len(_NEW_EVENT_TYPES)))
    params = {f"et{i}": et for i, et in enumerate(_NEW_EVENT_TYPES)}
    surviving = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM activity_log WHERE event_type IN ({placeholders})"),
        params,
    ).scalar_one()
    if surviving:
        joined = ", ".join(repr(v) for v in _NEW_EVENT_TYPES)
        raise RuntimeError(
            f"refusing to downgrade revision {revision}: "
            f"{surviving} row(s) in activity_log carry one of {{{joined}}} "
            "and would be invalidated by the older CHECK constraint. Clear "
            "them manually before re-running the downgrade: "
            f"DELETE FROM activity_log WHERE event_type IN ({joined});"
        )
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in("event_type", _EVENT_TYPES_PRE_123),
        )
