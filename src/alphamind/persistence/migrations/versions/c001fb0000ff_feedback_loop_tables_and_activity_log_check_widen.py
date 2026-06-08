"""feedback_loop_tables_and_activity_log_check_widen - six new feedback-loop tables +
activity_log event_type CHECK widening (ALP-879)

Revision ID: c001fb0000ff
Revises: b001fc0000ee
Create Date: 2026-06-07 00:00:00.000000

Creates the six new feedback-loop tables introduced by stories 02a-02d:

    agent_calls
    validations
    validation_outcomes
    retrospective_reports
    retrospective_decisions
    weekly_digest_snapshots

and widens the ``activity_log.event_type`` CHECK to admit the
``DISTILLATION_ANOMALY_FLAG`` vocabulary added by story 02e (ALP-877).

**CHECK-constraint vocabularies are frozen at this revision.** The migration
never reads live enums -- that is the CHECK-vocab trap: ``downgrade`` must narrow
back to exactly the pre-02e set, which a live-enum read could not produce once
``DISTILLATION_ANOMALY_FLAG`` exists. The ``agent_calls.error_class`` vocabulary
is frozen at the seven members current as of 04g/ALP-909 (``empty_response`` +
``internal_error`` added), so no later story needs a retroactive CHECK widen on
a deployed DB.

**Idempotent on a fresh DB.** The genesis baseline (``a000000000aa``) is
metadata-driven (``Base.metadata.create_all``), so on any fresh DB it already
creates all six tables and the wide ``activity_log.event_type`` CHECK (including
``DISTILLATION_ANOMALY_FLAG`` and all seven ``error_class`` members). ``upgrade``
guards each ``create_table`` and each ``create_index`` call; the CHECK-widen
guards on whether the persisted DDL already contains the new member. A fresh
``upgrade head`` is therefore a clean no-op that leaves every metadata-built
object untouched.

**Parented on** ``b001fc0000ee`` (ALP-900's ordinal-phase rename), so the chain
stays linear (a single head throughout).

**SQLite mechanics.** SQLite cannot ALTER a CHECK in place -- the widening is
performed via ``batch_alter_table(recreate="always")``, which drops the named
CHECK and adds it back over the new vocabulary. Migrations run with
``PRAGMA foreign_keys=OFF`` (see the alembic env), so the intermediate
``DROP TABLE`` does not trip incoming FK constraints.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from alphamind.persistence.migrations._check_rebuild import (
    check_in,
    persisted_check_has_member,
    rebuild_check,
)

# revision identifiers, used by Alembic.
revision: str = "c001fb0000ff"
down_revision: str | Sequence[str] | None = "b001fc0000ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------
# Frozen CHECK vocabularies (never read from live enums - the CHECK-vocab trap)
# ---------------------------------------------------------------------------

_AGENT_CALLS_ERROR_CLASS_VALUES: tuple[str, ...] = (
    "timeout",
    "malformed_output",
    "context_overflow",
    "model_api_error",
    "tool_use_error",
    "empty_response",
    "internal_error",
)

_VALIDATIONS_EXPECTED_DIRECTION_VALUES: tuple[str, ...] = (
    "improved",
    "unchanged",
    "degraded",
)

_VALIDATIONS_SUPERSEDED_REASON_VALUES: tuple[str, ...] = (
    "regime_transition",
    "model_version_change",
    "concurrent_edit_on_watched_artifact",
)

_VALIDATION_OUTCOMES_VERDICT_VALUES: tuple[str, ...] = (
    "improved",
    "degraded",
    "no_change",
    "inconclusive",
)

_VALIDATION_OUTCOMES_ROLLBACK_STATUS_VALUES: tuple[str, ...] = (
    "mandatory_clean_failure",
    "optional_pending_retrospective",
    "not_applicable",
)

_RETROSPECTIVE_DECISIONS_DECISION_TYPE_VALUES: tuple[str, ...] = (
    "promotion_candidate",
    "follow_up",
)

_RETROSPECTIVE_DECISIONS_VERDICT_VALUES: tuple[str, ...] = (
    "accepted",
    "rejected",
)

_ACTIVITY_LOG_EVENT_TYPE_OLD: tuple[str, ...] = (
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
    "BORROW_COST_ACCRUED",
    "GUARDRAIL_REJECTION",
    "RISK_LIMIT_APPROACHED",
    "RISK_PARAMETER_CHANGED",
    "EMERGENCY_INVOCATION_REQUESTED",
    "HALT_ACTIVATED",
    "HALT_LIFTED",
    "GREEKS_REFRESH_FAILED",
    "PM_DECISION",
    "COMMAND_ABANDONED",
    "ENVELOPE_PARSE_FAILED",
    "ENVELOPE_REJECTED",
    "CORPORATE_ACTION_APPLIED",
    "RECONCILIATION_ALERT",
    "RECONCILIATION_CORRECTION",
    "DISTILLATION_CONFIG_CHANGE",
    "PROFILE_SWITCHED",
)

_ACTIVITY_LOG_EVENT_TYPE_NEW: tuple[str, ...] = (
    *_ACTIVITY_LOG_EVENT_TYPE_OLD,
    "DISTILLATION_ANOMALY_FLAG",
)

_ACTIVITY_LOG_TABLE = "activity_log"
_ACTIVITY_LOG_EVENT_TYPE_CHECK_NAME = "ck_activity_log_event_type"
_ACTIVITY_LOG_NEW_MEMBER = "DISTILLATION_ANOMALY_FLAG"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# The CHECK-rebuild mechanics (``check_in`` / ``persisted_check_has_member`` /
# ``rebuild_check``) live in the shared ``migrations._check_rebuild`` module; the
# frozen vocabulary tuples above stay local to this revision (the CHECK-vocab trap).


def _rebuild_activity_log_event_type_check(values: tuple[str, ...]) -> None:
    """Rebuild the activity_log table to reset the event_type CHECK vocabulary.

    Routes the shared :func:`rebuild_check` mechanics over this migration's frozen
    *values*. Migrations run with ``PRAGMA foreign_keys=OFF`` (see the alembic env),
    so the intermediate ``DROP TABLE`` does not trip the incoming FKs on
    ``positions``, ``orders``, ``theses``, and ``invocations``.
    """
    rebuild_check(
        _ACTIVITY_LOG_TABLE,
        _ACTIVITY_LOG_EVENT_TYPE_CHECK_NAME,
        check_in("event_type", values),
    )


def _create_agent_calls(existing_tables: set[str], existing_indexes: set[str]) -> None:
    """Create the agent_calls table and its indexes (no-op if already present)."""
    if "agent_calls" not in existing_tables:
        op.create_table(
            "agent_calls",
            sa.Column("agent_call_id", sa.Text(), nullable=False),
            sa.Column("invocation_id", sa.Text(), nullable=False),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("model_id", sa.Text(), nullable=False),
            sa.Column("prompt_path", sa.Text(), nullable=False),
            sa.Column("prompt_git_sha", sa.Text(), nullable=False),
            sa.Column("prompt_content_hash", sa.Text(), nullable=False),
            sa.Column("sampling_params_json", sa.Text(), nullable=False),
            sa.Column("output_schema_ref", sa.Text(), nullable=True),
            sa.Column("tools_definition_ref", sa.Text(), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=False),
            sa.Column("output_tokens", sa.Integer(), nullable=False),
            sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
            sa.Column("wall_clock_ms", sa.Integer(), nullable=False),
            sa.Column("stop_reason", sa.Text(), nullable=False),
            sa.Column("success", sa.Boolean(), nullable=False),
            sa.Column("error_class", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("output_artifact_ref", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "error_class IS NULL OR "
                + check_in("error_class", _AGENT_CALLS_ERROR_CLASS_VALUES),
                name="ck_agent_calls_error_class",
            ),
            sa.ForeignKeyConstraint(
                ["invocation_id"],
                ["invocations.invocation_id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("agent_call_id"),
        )
    if "ix_agent_calls_invocation_id" not in existing_indexes:
        op.create_index("ix_agent_calls_invocation_id", "agent_calls", ["invocation_id"])
    if "ix_agent_calls_agent_name_attempt" not in existing_indexes:
        op.create_index(
            "ix_agent_calls_agent_name_attempt",
            "agent_calls",
            ["agent_name", "attempt_number"],
        )


def _create_validations(existing_tables: set[str], existing_indexes: set[str]) -> None:
    """Create the validations table and its indexes (no-op if already present)."""
    if "validations" not in existing_tables:
        op.create_table(
            "validations",
            sa.Column("validation_id", sa.Text(), nullable=False),
            sa.Column("registered_at", sa.Text(), nullable=False),
            sa.Column("registered_by_session_id", sa.Text(), nullable=True),
            sa.Column("edited_artifact", sa.Text(), nullable=False),
            sa.Column("pre_edit_version", sa.Text(), nullable=False),
            sa.Column("post_edit_version", sa.Text(), nullable=False),
            sa.Column("registered_regime", sa.Text(), nullable=False),
            sa.Column("registered_model_id", sa.Text(), nullable=False),
            sa.Column("watched_metric_ids_json", sa.Text(), nullable=False),
            sa.Column("window_length_days", sa.Integer(), nullable=False),
            sa.Column("expected_direction", sa.Text(), nullable=False),
            sa.Column("expected_magnitude", sa.Text(), nullable=False),
            sa.Column("success_criterion", sa.Text(), nullable=False),
            sa.Column("failure_criterion", sa.Text(), nullable=False),
            sa.Column("evaluation_due_at", sa.Text(), nullable=False),
            sa.Column("superseded_at", sa.Text(), nullable=True),
            sa.Column("superseded_reason", sa.Text(), nullable=True),
            sa.CheckConstraint(
                check_in("expected_direction", _VALIDATIONS_EXPECTED_DIRECTION_VALUES),
                name="ck_validations_expected_direction",
            ),
            sa.CheckConstraint(
                "superseded_reason IS NULL OR "
                + check_in("superseded_reason", _VALIDATIONS_SUPERSEDED_REASON_VALUES),
                name="ck_validations_superseded_reason",
            ),
            sa.PrimaryKeyConstraint("validation_id"),
        )
    if "ix_validations_edited_artifact" not in existing_indexes:
        op.create_index("ix_validations_edited_artifact", "validations", ["edited_artifact"])
    if "ix_validations_registered_at" not in existing_indexes:
        op.create_index("ix_validations_registered_at", "validations", ["registered_at"])
    if "ix_validations_superseded_at" not in existing_indexes:
        op.create_index("ix_validations_superseded_at", "validations", ["superseded_at"])


def _create_validation_outcomes(existing_tables: set[str], existing_indexes: set[str]) -> None:
    """Create the validation_outcomes table and its indexes (no-op if already present)."""
    if "validation_outcomes" not in existing_tables:
        op.create_table(
            "validation_outcomes",
            sa.Column("outcome_id", sa.Text(), nullable=False),
            sa.Column("validation_id", sa.Text(), nullable=False),
            sa.Column("evaluated_at", sa.Text(), nullable=False),
            sa.Column("evaluated_by_session_id", sa.Text(), nullable=True),
            sa.Column("verdict", sa.Text(), nullable=False),
            sa.Column("posterior_summary_json", sa.Text(), nullable=False),
            sa.Column("confounder_notes", sa.Text(), nullable=True),
            sa.Column("narrative", sa.Text(), nullable=False),
            sa.Column("rollback_status", sa.Text(), nullable=False),
            sa.UniqueConstraint("validation_id", name="uq_validation_outcomes_validation_id"),
            sa.CheckConstraint(
                check_in("verdict", _VALIDATION_OUTCOMES_VERDICT_VALUES),
                name="ck_validation_outcomes_verdict",
            ),
            sa.CheckConstraint(
                check_in("rollback_status", _VALIDATION_OUTCOMES_ROLLBACK_STATUS_VALUES),
                name="ck_validation_outcomes_rollback_status",
            ),
            sa.ForeignKeyConstraint(
                ["validation_id"],
                ["validations.validation_id"],
                name="fk_validation_outcomes_validation_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("outcome_id"),
        )
    if "ix_validation_outcomes_validation_id" not in existing_indexes:
        op.create_index(
            "ix_validation_outcomes_validation_id",
            "validation_outcomes",
            ["validation_id"],
        )
    if "ix_validation_outcomes_evaluated_at" not in existing_indexes:
        op.create_index(
            "ix_validation_outcomes_evaluated_at",
            "validation_outcomes",
            ["evaluated_at"],
        )


def _create_retrospective_reports(existing_tables: set[str], existing_indexes: set[str]) -> None:
    """Create the retrospective_reports table and its indexes (no-op if already present)."""
    if "retrospective_reports" not in existing_tables:
        op.create_table(
            "retrospective_reports",
            sa.Column("report_id", sa.Text(), nullable=False),
            sa.Column("window_start", sa.Text(), nullable=False),
            sa.Column("window_end", sa.Text(), nullable=False),
            sa.Column("generated_at", sa.Text(), nullable=False),
            sa.Column("generated_by_session_id", sa.Text(), nullable=True),
            sa.Column("report_file_ref", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("report_id"),
        )
    if "ix_retrospective_reports_generated_at" not in existing_indexes:
        op.create_index(
            "ix_retrospective_reports_generated_at",
            "retrospective_reports",
            ["generated_at"],
        )
    if "ix_retrospective_reports_window_start" not in existing_indexes:
        op.create_index(
            "ix_retrospective_reports_window_start",
            "retrospective_reports",
            ["window_start"],
        )


def _create_retrospective_decisions(existing_tables: set[str], existing_indexes: set[str]) -> None:
    """Create the retrospective_decisions table and its indexes (no-op if already present)."""
    if "retrospective_decisions" not in existing_tables:
        op.create_table(
            "retrospective_decisions",
            sa.Column("decision_id", sa.Text(), nullable=False),
            sa.Column("report_id", sa.Text(), nullable=False),
            sa.Column("captured_at", sa.Text(), nullable=False),
            sa.Column("decision_type", sa.Text(), nullable=False),
            sa.Column("item_identifier", sa.Text(), nullable=False),
            sa.Column("verdict", sa.Text(), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("linked_validation_id", sa.Text(), nullable=True),
            sa.CheckConstraint(
                check_in("decision_type", _RETROSPECTIVE_DECISIONS_DECISION_TYPE_VALUES),
                name="ck_retrospective_decisions_decision_type",
            ),
            sa.CheckConstraint(
                check_in("verdict", _RETROSPECTIVE_DECISIONS_VERDICT_VALUES),
                name="ck_retrospective_decisions_verdict",
            ),
            sa.ForeignKeyConstraint(
                ["report_id"],
                ["retrospective_reports.report_id"],
                name="fk_retrospective_decisions_report_id",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["linked_validation_id"],
                ["validations.validation_id"],
                name="fk_retrospective_decisions_linked_validation_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("decision_id"),
        )
    for idx_name, col in [
        ("ix_retrospective_decisions_report_id", "report_id"),
        ("ix_retrospective_decisions_captured_at", "captured_at"),
        ("ix_retrospective_decisions_decision_type", "decision_type"),
        ("ix_retrospective_decisions_linked_validation_id", "linked_validation_id"),
    ]:
        if idx_name not in existing_indexes:
            op.create_index(idx_name, "retrospective_decisions", [col])


def _create_weekly_digest_snapshots(existing_tables: set[str]) -> None:
    """Create the weekly_digest_snapshots table (no-op if already present)."""
    if "weekly_digest_snapshots" not in existing_tables:
        op.create_table(
            "weekly_digest_snapshots",
            sa.Column("snapshot_id", sa.Text(), nullable=False),
            sa.Column("week_start", sa.Text(), nullable=False),
            sa.Column("week_end", sa.Text(), nullable=False),
            sa.Column("snapshotted_at", sa.Text(), nullable=False),
            sa.Column("digest_schema_version", sa.Integer(), nullable=False),
            sa.Column("digest_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("week_start", name="uq_weekly_digest_snapshots_week_start"),
            sa.PrimaryKeyConstraint("snapshot_id"),
        )


def _existing_indexes(inspector: sa.engine.Inspector, table: str) -> set[str]:
    """Return the set of index names for *table*, or an empty set if it doesn't exist."""
    if not inspector.has_table(table):
        return set()
    return {str(i["name"]) for i in inspector.get_indexes(table)}


# ---------------------------------------------------------------------------
# upgrade - create six tables + widen activity_log event_type CHECK
# ---------------------------------------------------------------------------


def upgrade() -> None:
    """Create the six feedback-loop tables and widen the activity_log CHECK."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    _create_agent_calls(existing_tables, _existing_indexes(inspector, "agent_calls"))
    _create_validations(existing_tables, _existing_indexes(inspector, "validations"))
    _create_validation_outcomes(
        existing_tables, _existing_indexes(inspector, "validation_outcomes")
    )
    _create_retrospective_reports(
        existing_tables, _existing_indexes(inspector, "retrospective_reports")
    )
    _create_retrospective_decisions(
        existing_tables, _existing_indexes(inspector, "retrospective_decisions")
    )
    _create_weekly_digest_snapshots(existing_tables)

    if not persisted_check_has_member(bind, _ACTIVITY_LOG_TABLE, _ACTIVITY_LOG_NEW_MEMBER):
        _rebuild_activity_log_event_type_check(_ACTIVITY_LOG_EVENT_TYPE_NEW)


# ---------------------------------------------------------------------------
# downgrade - drop six tables + narrow activity_log event_type CHECK
# ---------------------------------------------------------------------------


def downgrade() -> None:
    """Drop the six feedback-loop tables and narrow the activity_log CHECK back."""
    bind = op.get_bind()
    if persisted_check_has_member(bind, _ACTIVITY_LOG_TABLE, _ACTIVITY_LOG_NEW_MEMBER):
        _rebuild_activity_log_event_type_check(_ACTIVITY_LOG_EVENT_TYPE_OLD)

    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table in [
        "retrospective_decisions",
        "validation_outcomes",
        "weekly_digest_snapshots",
        "retrospective_reports",
        "validations",
        "agent_calls",
    ]:
        if table in existing_tables:
            op.drop_table(table)
