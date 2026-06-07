"""Tests for _TRIGGER_SOURCES vocabulary and CHECK constraint in invocations table.

Verifies that:
- ``continuous_monitor`` is present in the _TRIGGER_SOURCES tuple (not accidentally removed).
- An InvocationRow with a known-good trigger_source can be inserted against an
  in-memory SQLite engine with the CHECK constraint active.
- An InvocationRow with an unrecognized trigger_source raises IntegrityError
  (the constraint is enforced).
- ``borrow_accrual`` is NOT in _TRIGGER_SOURCES (story 04b relocated borrow accrual
  into the pipeline fill-collection write unit; it no longer mints its own InvocationRow —
  ALP-842 CU4).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full schema."""
    import alphamind.state.tables  # noqa: F401 — registers tables on Base.metadata

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
# Minimal data builders — must satisfy all NOT NULL + FK constraints.
# ---------------------------------------------------------------------------


def _process_lifetime_kwargs(process_lifetime_id: str = "proc-1") -> dict[str, object]:
    return {
        "process_lifetime_id": process_lifetime_id,
        "process_role": "pipeline",
        "process_start_at": "2026-05-27T16:00:00Z",
        "process_pid": 9999,
        "hostname": "alpha-prod-01",
        "git_sha": "abcdef1234567890" * 2 + "abcd",
        "git_branch": "main",
        "git_dirty": 0,
        "python_version": "3.13.1",
        "pip_freeze_hash": "0" * 64,
        "pip_freeze_snapshot_path": "/tmp/pip_freeze.txt",
        "anthropic_sdk_version": "0.40.0",
        "claude_agent_sdk_version": "0.1.69",
        "os_release": "Linux-6.5.0-x86_64",
    }


def _invocation_kwargs(
    trigger_source: str = "continuous_monitor",
    invocation_id: str = "inv-test-001",
    process_lifetime_id: str = "proc-1",
) -> dict[str, object]:
    return {
        "invocation_id": invocation_id,
        "process_lifetime_id": process_lifetime_id,
        "start_at": "2026-05-27T16:00:00Z",
        "fill_collection_completed_at": None,
        "command_execution_completed_at": None,
        "trigger_type": "scheduled",
        "trigger_source": trigger_source,
        "trigger_reason": "test_tick",
        "git_sha_at_invocation": "abcdef1234567890" * 2 + "abcd",
        "active_profile": "medium",
        "active_regime": "normal",
        "active_mode": "normal",
        "active_overlays_json": "[]",
        "resolved_config_hash": "0" * 64,
        "resolved_config_snapshot_path": "/tmp/resolved_config.json",
        "feature_flags_snapshot_json": "{}",
        "data_calibration_state_snapshot_path": "/tmp/data_calibration_state.json",
        "data_source_freshness_json": "{}",
        "fill_collection_summary_json": None,
        "command_execution_summary_json": None,
        "staleness_flag": None,
        "snapshot_metadata_json": None,
    }


class TestTriggerSourcesVocabulary:
    """Unit-level checks against the _TRIGGER_SOURCES tuple — no DB needed."""

    def test_borrow_accrual_not_in_trigger_sources(self) -> None:
        """Story 04b relocated borrow accrual into the pipeline; source is dead (ALP-842 CU4)."""
        from alphamind.state.tables.invocations import _TRIGGER_SOURCES

        assert "borrow_accrual" not in _TRIGGER_SOURCES

    def test_trigger_sources_still_contains_continuous_monitor(self) -> None:
        """The adjacent continuous_monitor value was not accidentally removed."""
        from alphamind.state.tables.invocations import _TRIGGER_SOURCES

        assert "continuous_monitor" in _TRIGGER_SOURCES

    def test_trigger_sources_is_tuple(self) -> None:
        from alphamind.state.tables.invocations import _TRIGGER_SOURCES

        assert isinstance(_TRIGGER_SOURCES, tuple)


class TestInvocationRowTriggerSourceConstraint:
    """INSERT-level tests against in-memory SQLite with the CHECK constraint active."""

    def test_insert_known_trigger_source_succeeds(self, session: Session) -> None:
        """AC: Inserting an InvocationRow with a known trigger_source succeeds."""
        from alphamind.state.tables.invocations import InvocationRow
        from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

        session.add(ProcessLifetimeRow(**_process_lifetime_kwargs()))
        session.commit()

        row = InvocationRow(**_invocation_kwargs(trigger_source="continuous_monitor"))
        session.add(row)
        session.commit()  # must not raise

        readback = session.get(InvocationRow, "inv-test-001")
        assert readback is not None
        assert readback.trigger_source == "continuous_monitor"

    def test_insert_unrecognized_trigger_source_raises_integrity_error(
        self, session: Session
    ) -> None:
        """AC: Unrecognized trigger_source raises IntegrityError (constraint not loosened)."""
        from alphamind.state.tables.invocations import InvocationRow
        from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

        session.add(ProcessLifetimeRow(**_process_lifetime_kwargs()))
        session.commit()

        row = InvocationRow(
            **_invocation_kwargs(
                trigger_source="totally_invalid_source",
                invocation_id="inv-bad-001",
            )
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()
