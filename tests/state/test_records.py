"""Tests for the typed Pydantic façade over process-lifetime / invocation rows.

Story 02b: ``ProcessLifetimeRecord`` and ``InvocationRecord`` are the typed
handles callers operate on; the SQLAlchemy rows are the storage shape. The
two adapters round-trip without loss — this test pins the equivalence so a
future schema or record drift fails loudly here rather than silently in a
downstream join.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_from_row,
    invocation_record_to_row,
    process_lifetime_record_from_row,
    process_lifetime_record_to_row,
)


@pytest.fixture()
def engine() -> Iterator[Engine]:
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


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


def _make_invocation_record(
    invocation_id: str = "inv-2026-05-07T14:30:00Z-abcd",
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at="2026-05-07T14:30:00Z",
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json='["pre-event"]',
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/resolved_config.json"
        ),
        feature_flags_snapshot_json='{"foo": true}',
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/data_calibration_state.json"
        ),
        data_source_freshness_json='{"polygon": "2026-05-07T14:00:00Z"}',
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


class TestProcessLifetimeRecord:
    def test_record_is_frozen(self) -> None:
        rec = _make_process_lifetime_record()
        with pytest.raises((ValueError, TypeError)):
            rec.process_role = "monitor"

    def test_round_trip_through_sqlalchemy(self, session: Session) -> None:
        record = _make_process_lifetime_record()
        row = process_lifetime_record_to_row(record)
        session.add(row)
        session.commit()

        from alphamind.state.tables.process_lifetimes import (
            ProcessLifetimeRow,
        )

        readback_row = session.get(ProcessLifetimeRow, "proc-1")
        assert readback_row is not None
        readback_record = process_lifetime_record_from_row(readback_row)

        assert readback_record == record

    def test_record_rejects_unknown_role(self) -> None:
        bad = _make_process_lifetime_record().model_dump() | {"process_role": "rogue_process"}
        with pytest.raises((ValueError, TypeError)):
            ProcessLifetimeRecord.model_validate(bad)


class TestInvocationRecord:
    def test_record_is_frozen(self) -> None:
        rec = _make_invocation_record()
        with pytest.raises((ValueError, TypeError)):
            rec.trigger_type = "manual"

    def test_round_trip_through_sqlalchemy(self, session: Session) -> None:

        # Parent process_lifetime row first, FK requires it.
        parent = process_lifetime_record_to_row(_make_process_lifetime_record())
        session.add(parent)
        session.commit()
        # Detach the parent because re-using its mapped instance after a
        # commit triggers SQLAlchemy's stale-state warnings; the FK only
        # cares about the value.
        del parent

        record = _make_invocation_record()
        row = invocation_record_to_row(record)
        session.add(row)
        session.commit()

        from alphamind.state.tables.invocations import InvocationRow

        readback_row = session.get(InvocationRow, "inv-2026-05-07T14:30:00Z-abcd")
        assert readback_row is not None
        readback_record = invocation_record_from_row(readback_row)
        assert readback_record == record

        # Verify Phase 1/2 columns can be filled with strings on a later
        # update without breaking the round-trip.
        readback_row.phase1_completed_at = "2026-05-07T14:31:00Z"
        readback_row.staleness_flag = 0
        session.commit()
        re_readback_row = session.get(InvocationRow, "inv-2026-05-07T14:30:00Z-abcd")
        assert re_readback_row is not None
        re_readback = invocation_record_from_row(re_readback_row)
        assert re_readback.phase1_completed_at == "2026-05-07T14:31:00Z"
        assert re_readback.staleness_flag is False

    def test_record_rejects_unknown_trigger_type(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            InvocationRecord.model_validate(
                _make_invocation_record().model_dump() | {"trigger_type": "automated"}
            )

    def test_record_rejects_unknown_active_mode(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            InvocationRecord.model_validate(
                _make_invocation_record().model_dump() | {"active_mode": "frozen"}
            )
