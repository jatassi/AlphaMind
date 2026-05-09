"""extend activity_log.event_type CHECK with envelope_parse_failed

Revision ID: d4a8e9f2c1b3
Revises: f7a9d3c2e5b1
Create Date: 2026-05-08 00:00:00.000000

Story 08 / ALP-366 adds the ``ENVELOPE_PARSE_FAILED`` event type to capture
Layer-1 (Pydantic) parse failures from the engine-stub ``submit_envelope``
MCP wrapper. The activity-log CHECK constraint must accept the new value so
the Phase 2 write path can persist it alongside the existing event vocabulary.

SQLite does not support direct CHECK-constraint alteration, so the upgrade
recreates the constraint via :func:`op.batch_alter_table`. The downgrade
refuses to run if any rows already carry the new event type to avoid silently
invalidating accumulated audit history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a8e9f2c1b3"
down_revision: str | Sequence[str] | None = "f7a9d3c2e5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Hardcoded vocabularies pinned to this revision so future EventType
# additions do not silently shift this migration's payload.
_EVENT_TYPES_PRE_366 = (
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
    "CORPORATE_ACTION_APPLIED",
    "DISTILLATION_CONFIG_CHANGE",
)

_NEW_EVENT_TYPE = "ENVELOPE_PARSE_FAILED"


def _check_in(values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"event_type IN ({rendered})"


def upgrade() -> None:
    """Replace the event_type CHECK constraint with one that accepts the new value."""
    new_vocab = (*_EVENT_TYPES_PRE_366, _NEW_EVENT_TYPE)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_event_type",
            _check_in(new_vocab),
        )


def downgrade() -> None:
    """Revert to the pre-ALP-366 event_type vocabulary.

    Refuses to downgrade when any row already carries the new event type,
    so a routine ``alembic downgrade -1`` cannot silently invalidate
    accumulated PM-tool audit history.
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
            _check_in(_EVENT_TYPES_PRE_366),
        )
