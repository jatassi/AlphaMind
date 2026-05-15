"""Schema-level tests for ``process_lifetimes`` and ``invocations`` (story 02b).

Verifies column shape, CHECK constraints, and the FK on
``invocations.process_lifetime_id`` against an in-memory SQLite engine with the
same pragmas (foreign_keys=ON, WAL, busy_timeout, synchronous=NORMAL) used in
production.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full schema."""
    # Side-effect import: registers the new tables on ``Base.metadata`` so
    # ``create_all`` materializes them. Importing here keeps test-time
    # registration explicit and avoids hidden ``persistence.models`` edits.
    import alphamind.state.tables  # noqa: F401

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------


def _process_lifetime_kwargs(process_lifetime_id: str = "proc-1") -> dict[str, object]:
    return {
        "process_lifetime_id": process_lifetime_id,
        "process_role": "pipeline",
        "process_start_at": "2026-05-07T14:30:00Z",
        "process_pid": 12345,
        "hostname": "alpha-prod-01",
        "git_sha": "abcdef1234567890" * 2 + "abcd",
        "git_branch": "main",
        "git_dirty": 0,
        "python_version": "3.13.1",
        "pip_freeze_hash": "0" * 64,
        "pip_freeze_snapshot_path": "/tmp/provenance/process_lifetimes/proc-1/pip_freeze.txt",
        "anthropic_sdk_version": "0.40.0",
        "claude_agent_sdk_version": "0.1.69",
        "os_release": "Linux-6.5.0-generic-x86_64",
    }


def _invocation_kwargs(
    invocation_id: str = "inv-2026-05-07T14:30:00Z-abcd",
    process_lifetime_id: str = "proc-1",
) -> dict[str, object]:
    return {
        "invocation_id": invocation_id,
        "process_lifetime_id": process_lifetime_id,
        "start_at": "2026-05-07T14:30:00Z",
        "phase1_completed_at": None,
        "phase2_completed_at": None,
        "trigger_type": "scheduled",
        "trigger_source": "morning-cron",
        "trigger_reason": "0 9 * * 1-5",
        "git_sha_at_invocation": "abcdef1234567890" * 2 + "abcd",
        "active_profile": "medium",
        "active_regime": "normal",
        "active_mode": "normal",
        "active_overlays_json": '["pre-event"]',
        "resolved_config_hash": "0" * 64,
        "resolved_config_snapshot_path": (
            "/tmp/provenance/invocations/" + invocation_id + "/resolved_config.json"
        ),
        "feature_flags_snapshot_json": '{"foo": true}',
        "data_calibration_state_snapshot_path": (
            "/tmp/provenance/invocations/" + invocation_id + "/data_calibration_state.json"
        ),
        "data_source_freshness_json": '{"polygon": "2026-05-07T14:00:00Z"}',
        "fill_collection_summary_json": None,
        "command_execution_summary_json": None,
        "staleness_flag": None,
        "snapshot_metadata_json": None,
    }


# ---------------------------------------------------------------------------
# process_lifetimes
# ---------------------------------------------------------------------------


class TestProcessLifetimesTable:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("process_lifetimes")}
        expected = {
            "process_lifetime_id",
            "process_role",
            "process_start_at",
            "process_pid",
            "hostname",
            "git_sha",
            "git_branch",
            "git_dirty",
            "python_version",
            "pip_freeze_hash",
            "pip_freeze_snapshot_path",
            "anthropic_sdk_version",
            "claude_agent_sdk_version",
            "os_release",
        }
        assert set(cols) == expected
        # Exactly one primary key, on process_lifetime_id.
        pk = insp.get_pk_constraint("process_lifetimes")
        assert pk["constrained_columns"] == ["process_lifetime_id"]

    def test_round_trip_persists_all_columns(self, session: Session) -> None:
        from alphamind.state.tables.process_lifetimes import (
            ProcessLifetimeRow,
        )

        row = ProcessLifetimeRow(**_process_lifetime_kwargs())
        session.add(row)
        session.commit()

        readback = session.get(ProcessLifetimeRow, "proc-1")
        assert readback is not None
        assert readback.process_role == "pipeline"
        assert readback.process_pid == 12345
        assert readback.git_dirty == 0
        assert readback.os_release.startswith("Linux")

    def test_check_rejects_unknown_process_role(self, session: Session) -> None:
        from alphamind.state.tables.process_lifetimes import (
            ProcessLifetimeRow,
        )

        kwargs = _process_lifetime_kwargs()
        kwargs["process_role"] = "unknown"
        session.add(ProcessLifetimeRow(**kwargs))
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# invocations
# ---------------------------------------------------------------------------


class TestInvocationsTable:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("invocations")}
        expected = {
            "invocation_id",
            "process_lifetime_id",
            "start_at",
            "phase1_completed_at",
            "phase2_completed_at",
            "trigger_type",
            "trigger_source",
            "trigger_reason",
            "git_sha_at_invocation",
            "active_profile",
            "active_regime",
            "active_mode",
            "active_overlays_json",
            "resolved_config_hash",
            "resolved_config_snapshot_path",
            "feature_flags_snapshot_json",
            "data_calibration_state_snapshot_path",
            "data_source_freshness_json",
            "fill_collection_summary_json",
            "command_execution_summary_json",
            "staleness_flag",
            "snapshot_metadata_json",
        }
        assert set(cols) == expected
        pk = insp.get_pk_constraint("invocations")
        assert pk["constrained_columns"] == ["invocation_id"]

    def test_invocation_fk_blocks_orphan_insert(self, session: Session) -> None:
        from alphamind.state.tables.invocations import InvocationRow

        # No process_lifetimes row inserted; the FK must reject this.
        session.add(InvocationRow(**_invocation_kwargs()))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_invocation_fk_restricts_delete_of_referenced_lifetime(self, session: Session) -> None:
        from alphamind.state.tables.invocations import InvocationRow
        from alphamind.state.tables.process_lifetimes import (
            ProcessLifetimeRow,
        )

        session.add(ProcessLifetimeRow(**_process_lifetime_kwargs()))
        session.commit()
        session.add(InvocationRow(**_invocation_kwargs()))
        session.commit()

        # Cannot delete the parent while a child invocation references it.
        with pytest.raises(IntegrityError):
            session.execute(
                text("DELETE FROM process_lifetimes WHERE process_lifetime_id = :pid"),
                {"pid": "proc-1"},
            )
            session.commit()

    def test_check_rejects_unknown_trigger_type(self, session: Session) -> None:
        from alphamind.state.tables.invocations import InvocationRow
        from alphamind.state.tables.process_lifetimes import (
            ProcessLifetimeRow,
        )

        session.add(ProcessLifetimeRow(**_process_lifetime_kwargs()))
        session.commit()

        kwargs = _invocation_kwargs()
        kwargs["trigger_type"] = "automated"
        session.add(InvocationRow(**kwargs))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_active_mode(self, session: Session) -> None:
        from alphamind.state.tables.invocations import InvocationRow
        from alphamind.state.tables.process_lifetimes import (
            ProcessLifetimeRow,
        )

        session.add(ProcessLifetimeRow(**_process_lifetime_kwargs()))
        session.commit()

        kwargs = _invocation_kwargs()
        kwargs["active_mode"] = "frozen"
        session.add(InvocationRow(**kwargs))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_round_trip_persists_all_columns(self, session: Session) -> None:
        from alphamind.state.tables.invocations import InvocationRow
        from alphamind.state.tables.process_lifetimes import (
            ProcessLifetimeRow,
        )

        session.add(ProcessLifetimeRow(**_process_lifetime_kwargs()))
        session.commit()

        session.add(InvocationRow(**_invocation_kwargs()))
        session.commit()

        readback = session.get(InvocationRow, "inv-2026-05-07T14:30:00Z-abcd")
        assert readback is not None
        assert readback.process_lifetime_id == "proc-1"
        assert readback.trigger_type == "scheduled"
        assert readback.active_mode == "normal"
        assert readback.active_overlays_json == '["pre-event"]'
        assert readback.phase1_completed_at is None
