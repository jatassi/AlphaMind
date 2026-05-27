"""SQLAlchemy mapping for the ``invocations`` table (story 02b).

One row per pipeline invocation. The row carries the execution scaffolding
(timestamps), the trigger metadata, the code-state field, the resolved
composition snapshot references, and the data-layer state references the
feedback loop joins against to isolate confounders during prompt-edit and
config-change validation.

Phase 1 / Phase 2 completion timestamps and their summary JSON columns are
nullable on insert; the ``InvocationContext`` writes the row on enter and
the Phase 1 / Phase 2 write paths (later stories) UPDATE these columns
inside their own transactions.

Foreign key on ``process_lifetime_id`` with ``ON DELETE RESTRICT`` —
process-lifetime rows are append-only, and a parent row referenced by an
invocation cannot be deleted without first removing the invocation.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base

# Trigger and active-mode vocabularies mirrored in
# ``state-persistence.md`` § Invocation records. CHECK constraints defend a
# future direct-SQL writer (e.g. a backfill script) against drifting from
# the typed-record vocabulary.
_TRIGGER_TYPES = ("scheduled", "emergency", "manual")
_ACTIVE_MODES = ("normal", "defensive_posture", "halted")

# Recognized ``trigger_source`` values. The command-center activity-log
# explorer's "Operator actions" saved filter view joins activity_log
# rows against invocations rows whose ``trigger_source`` equals
# ``operator_console`` (per docs/design/command-center.md § Operator
# actions); the CHECK below pins the value so a downstream writer
# cannot accidentally drop it via a typo. Other allowed values follow
# the documented vocabulary across the scheduler, monitor, replay
# harness, and CLI dispatch paths (F15).
_TRIGGER_SOURCES = (
    # Operator + manual dispatch.
    "cli",
    "debug_e2e_cli",
    "operator_console",
    # Continuous monitor — emergency dispatch and scheduled ticks.
    "continuous_monitor",
    # Daily borrow-accrual tick emitted by the continuous monitor (ALP-718 / ALP-715 § 4f).
    "borrow_accrual",
    # Replay harness (distillation back-tests).
    "replay",
    # Scheduler cron triggers — every RunType member doubles as a
    # trigger_source via scheduler/driver.py threading ``trigger_key``
    # through. Keep in sync with ``alphamind.config.models.run_types.RunType``.
    "pre_open",
    "market_hours_rolling",
    "pre_close",
    "off_hours_rolling",
    "weekend_saturday",
    "weekend_sunday",
    "emergency",
    # Test fixtures — many tests construct InvocationRow with synthetic
    # values. Keep these here so the CHECK doesn't break the test suite.
    # When a future refactor normalizes test fixtures to a single marker,
    # the additional entries can shrink.
    "test",
    "cron",
    "morning-cron",
    "test_initial_greeks_persistence.py",
)


class InvocationRow(Base):
    """Forward-only per-invocation row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Invocation
    records. The 22 columns subdivide into execution scaffolding, trigger,
    code state, composition state, and data-layer state groups.
    """

    __tablename__ = "invocations"

    invocation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    process_lifetime_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("process_lifetimes.process_lifetime_id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_at: Mapped[str] = mapped_column(Text, nullable=False)
    phase1_completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase2_completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_source: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    git_sha_at_invocation: Mapped[str] = mapped_column(Text, nullable=False)
    active_profile: Mapped[str] = mapped_column(Text, nullable=False)
    active_regime: Mapped[str] = mapped_column(Text, nullable=False)
    active_mode: Mapped[str] = mapped_column(Text, nullable=False)
    active_overlays_json: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_config_snapshot_path: Mapped[str] = mapped_column(Text, nullable=False)
    feature_flags_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    data_calibration_state_snapshot_path: Mapped[str] = mapped_column(Text, nullable=False)
    data_source_freshness_json: Mapped[str] = mapped_column(Text, nullable=False)
    fill_collection_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_execution_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    staleness_flag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"trigger_type IN ({', '.join(repr(t) for t in _TRIGGER_TYPES)})",
            name="ck_invocations_trigger_type",
        ),
        CheckConstraint(
            f"trigger_source IN ({', '.join(repr(s) for s in _TRIGGER_SOURCES)})",
            name="ck_invocations_trigger_source",
        ),
        CheckConstraint(
            f"active_mode IN ({', '.join(repr(m) for m in _ACTIVE_MODES)})",
            name="ck_invocations_active_mode",
        ),
        CheckConstraint(
            "staleness_flag IS NULL OR staleness_flag IN (0, 1)",
            name="ck_invocations_staleness_flag",
        ),
    )
