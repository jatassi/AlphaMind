"""Typed Pydantic façade over the ``process_lifetimes`` and ``invocations`` rows.

These frozen records are the handles callers operate on; the SQLAlchemy
rows in ``alphamind.state.tables`` are the storage
shape. Pure adapter functions translate between the two so callers never
import the ORM rows directly.

The records mirror the column shape the design doc prescribes (see
``docs/design/05-execution-layer/state-persistence.md`` § Process lifetimes
and § Invocation records). Vocabulary fields are typed as ``Literal`` so
mistakes surface at validation time rather than at SQL CHECK time.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict

from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

ProcessRole = Literal["pipeline", "monitor"]
TriggerType = Literal["scheduled", "emergency", "manual"]
ActiveMode = Literal["normal", "defensive_posture", "halted"]


class ProcessLifetimeRecord(BaseModel):
    """Frozen typed handle for a ``process_lifetimes`` row.

    Mirrors the field list in ``state-persistence.md`` § Process lifetimes.
    Booleans (``git_dirty``) are real ``bool``; the SQL row stores them as
    ``INTEGER`` 0/1 because SQLite has no native BOOLEAN type.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    process_lifetime_id: str
    process_role: ProcessRole
    process_start_at: str
    process_pid: int
    hostname: str
    git_sha: str
    git_branch: str
    git_dirty: bool
    python_version: str
    pip_freeze_hash: str
    pip_freeze_snapshot_path: str
    anthropic_sdk_version: str
    claude_agent_sdk_version: str
    os_release: str


class InvocationRecord(BaseModel):
    """Frozen typed handle for an ``invocations`` row.

    Mirrors the field list in ``state-persistence.md`` § Invocation
    records. Phase 1 / Phase 2 timestamps and their summary JSON columns
    are ``None`` at insert time and filled later by the Phase 1 / Phase 2
    write paths.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    invocation_id: str
    process_lifetime_id: str
    start_at: str
    phase1_completed_at: str | None
    phase2_completed_at: str | None
    trigger_type: TriggerType
    trigger_source: str
    trigger_reason: str
    git_sha_at_invocation: str
    active_profile: str
    active_regime: str
    active_mode: ActiveMode
    active_overlays_json: str
    resolved_config_hash: str
    resolved_config_snapshot_path: str
    feature_flags_snapshot_json: str
    data_calibration_state_snapshot_path: str
    data_source_freshness_json: str
    fill_collection_summary_json: str | None
    command_execution_summary_json: str | None
    staleness_flag: bool | None
    snapshot_metadata_json: str | None


# ---------------------------------------------------------------------------
# Adapters — pure, zero-side-effect translations both ways
# ---------------------------------------------------------------------------


def process_lifetime_record_to_row(record: ProcessLifetimeRecord) -> ProcessLifetimeRow:
    """Build a ``ProcessLifetimeRow`` from a typed record."""
    return ProcessLifetimeRow(
        process_lifetime_id=record.process_lifetime_id,
        process_role=record.process_role,
        process_start_at=record.process_start_at,
        process_pid=record.process_pid,
        hostname=record.hostname,
        git_sha=record.git_sha,
        git_branch=record.git_branch,
        git_dirty=1 if record.git_dirty else 0,
        python_version=record.python_version,
        pip_freeze_hash=record.pip_freeze_hash,
        pip_freeze_snapshot_path=record.pip_freeze_snapshot_path,
        anthropic_sdk_version=record.anthropic_sdk_version,
        claude_agent_sdk_version=record.claude_agent_sdk_version,
        os_release=record.os_release,
    )


def process_lifetime_record_from_row(row: ProcessLifetimeRow) -> ProcessLifetimeRecord:
    """Build a typed record from a ``ProcessLifetimeRow``."""
    return ProcessLifetimeRecord(
        process_lifetime_id=row.process_lifetime_id,
        process_role=_assert_member(row.process_role, ProcessRole, field="process_role"),
        process_start_at=row.process_start_at,
        process_pid=row.process_pid,
        hostname=row.hostname,
        git_sha=row.git_sha,
        git_branch=row.git_branch,
        git_dirty=bool(row.git_dirty),
        python_version=row.python_version,
        pip_freeze_hash=row.pip_freeze_hash,
        pip_freeze_snapshot_path=row.pip_freeze_snapshot_path,
        anthropic_sdk_version=row.anthropic_sdk_version,
        claude_agent_sdk_version=row.claude_agent_sdk_version,
        os_release=row.os_release,
    )


def invocation_record_to_row(record: InvocationRecord) -> InvocationRow:
    """Build an ``InvocationRow`` from a typed record."""
    return InvocationRow(
        invocation_id=record.invocation_id,
        process_lifetime_id=record.process_lifetime_id,
        start_at=record.start_at,
        phase1_completed_at=record.phase1_completed_at,
        phase2_completed_at=record.phase2_completed_at,
        trigger_type=record.trigger_type,
        trigger_source=record.trigger_source,
        trigger_reason=record.trigger_reason,
        git_sha_at_invocation=record.git_sha_at_invocation,
        active_profile=record.active_profile,
        active_regime=record.active_regime,
        active_mode=record.active_mode,
        active_overlays_json=record.active_overlays_json,
        resolved_config_hash=record.resolved_config_hash,
        resolved_config_snapshot_path=record.resolved_config_snapshot_path,
        feature_flags_snapshot_json=record.feature_flags_snapshot_json,
        data_calibration_state_snapshot_path=record.data_calibration_state_snapshot_path,
        data_source_freshness_json=record.data_source_freshness_json,
        fill_collection_summary_json=record.fill_collection_summary_json,
        command_execution_summary_json=record.command_execution_summary_json,
        staleness_flag=_bool_to_int_or_none(record.staleness_flag),
        snapshot_metadata_json=record.snapshot_metadata_json,
    )


def invocation_record_from_row(row: InvocationRow) -> InvocationRecord:
    """Build a typed record from an ``InvocationRow``."""
    return InvocationRecord(
        invocation_id=row.invocation_id,
        process_lifetime_id=row.process_lifetime_id,
        start_at=row.start_at,
        phase1_completed_at=row.phase1_completed_at,
        phase2_completed_at=row.phase2_completed_at,
        trigger_type=_assert_member(row.trigger_type, TriggerType, field="trigger_type"),
        trigger_source=row.trigger_source,
        trigger_reason=row.trigger_reason,
        git_sha_at_invocation=row.git_sha_at_invocation,
        active_profile=row.active_profile,
        active_regime=row.active_regime,
        active_mode=_assert_member(row.active_mode, ActiveMode, field="active_mode"),
        active_overlays_json=row.active_overlays_json,
        resolved_config_hash=row.resolved_config_hash,
        resolved_config_snapshot_path=row.resolved_config_snapshot_path,
        feature_flags_snapshot_json=row.feature_flags_snapshot_json,
        data_calibration_state_snapshot_path=row.data_calibration_state_snapshot_path,
        data_source_freshness_json=row.data_source_freshness_json,
        fill_collection_summary_json=row.fill_collection_summary_json,
        command_execution_summary_json=row.command_execution_summary_json,
        staleness_flag=None if row.staleness_flag is None else bool(row.staleness_flag),
        snapshot_metadata_json=row.snapshot_metadata_json,
    )


# ---------------------------------------------------------------------------
# Vocabulary guards — defend the read path against direct-SQL inserts that
# bypassed the schema CHECKs (e.g. a backfill script).
# ---------------------------------------------------------------------------


def _assert_member(value: str, literal_type: Any, *, field: str) -> Any:
    """Coerce ``value`` to a member of *literal_type* or raise ``ValueError``.

    ``literal_type`` is a ``typing.Literal[...]`` alias; the valid set is read
    via :func:`typing.get_args` so the literal definition stays the single
    source of truth for the vocabulary. The return is typed ``Any`` because
    ``Literal`` aliases are not first-class generic parameters in Python's
    typing system; callers narrow at the call site (the typed Pydantic
    constructor that consumes the result handles validation downstream).
    """
    valid = get_args(literal_type)
    if value not in valid:
        msg = f"unknown {field}={value!r} (expected one of {valid})"
        raise ValueError(msg)
    return value


def _bool_to_int_or_none(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0
