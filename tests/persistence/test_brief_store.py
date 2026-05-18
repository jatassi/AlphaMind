"""Tests for :mod:`alphamind.persistence.brief_store` (story ALP-518)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import alphamind.state.tables  # noqa: F401  — registers invocations table for FK target
from alphamind.persistence.brief_store import StoredBrief, load_brief
from alphamind.persistence.models import Base, Brief
from alphamind.state.tables.invocations import InvocationRow

_INVOCATION_ID = "20260517T220000Z-test"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine: Engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        _seed_process_and_invocation(sess)
        yield sess
    engine.dispose()


def _seed_process_and_invocation(session: Session) -> None:
    """Seed one process-lifetime + one invocation so the briefs FK passes."""
    session.execute(
        Base.metadata.tables["process_lifetimes"].insert(),
        {
            "process_lifetime_id": "pl-test",
            "process_role": "pipeline",
            "process_start_at": "2026-05-17T22:00:00Z",
            "process_pid": 1234,
            "hostname": "test",
            "git_sha": "a" * 40,
            "git_branch": "test",
            "git_dirty": 0,
            "python_version": "3.13.0",
            "pip_freeze_hash": "0" * 64,
            "pip_freeze_snapshot_path": "/tmp/pip.txt",
            "anthropic_sdk_version": "0.0.0",
            "claude_agent_sdk_version": "0.0.0",
            "os_release": "test",
        },
    )
    session.add(
        InvocationRow(
            invocation_id=_INVOCATION_ID,
            process_lifetime_id="pl-test",
            start_at="2026-05-17T22:00:00Z",
            phase1_completed_at=None,
            phase2_completed_at=None,
            trigger_type="scheduled",
            trigger_source="test",
            trigger_reason="seed",
            git_sha_at_invocation="a" * 40,
            active_profile="medium",
            active_regime="normal",
            active_mode="normal",
            active_overlays_json="[]",
            resolved_config_hash="0" * 64,
            resolved_config_snapshot_path="/tmp/r.json",
            feature_flags_snapshot_json="{}",
            data_calibration_state_snapshot_path="/tmp/d.json",
            data_source_freshness_json="{}",
            fill_collection_summary_json=None,
            command_execution_summary_json=None,
            staleness_flag=None,
            snapshot_metadata_json=None,
        )
    )
    session.commit()


def test_load_brief_returns_none_when_absent(session: Session) -> None:
    """No row → ``None``; the helper does not raise."""
    assert (
        load_brief(session, invocation_id=_INVOCATION_ID, brief_kind="correlation_regime") is None
    )


def test_load_brief_roundtrips_inserted_row(session: Session) -> None:
    """A row inserted under ``(invocation_id, brief_kind)`` rehydrates cleanly."""
    reference_index = {"CR-1": "regime.label", "CR-2": "q7.pair::HYG-SPY"}
    session.add(
        Brief(
            invocation_id=_INVOCATION_ID,
            brief_kind="correlation_regime",
            reference_index_json=json.dumps(reference_index, sort_keys=True),
            text="CORRELATION & REGIME BRIEF\n...",
            created_at="2026-05-17T22:30:00Z",
        )
    )
    session.commit()

    loaded = load_brief(session, invocation_id=_INVOCATION_ID, brief_kind="correlation_regime")

    assert loaded == StoredBrief(
        invocation_id=_INVOCATION_ID,
        brief_kind="correlation_regime",
        text="CORRELATION & REGIME BRIEF\n...",
        reference_index=reference_index,
        created_at="2026-05-17T22:30:00Z",
    )


def test_load_brief_keys_by_kind(session: Session) -> None:
    """A different ``brief_kind`` under the same invocation returns ``None``."""
    session.add(
        Brief(
            invocation_id=_INVOCATION_ID,
            brief_kind="correlation_regime",
            reference_index_json="{}",
            text="body",
            created_at="2026-05-17T22:30:00Z",
        )
    )
    session.commit()
    # The CHECK constraint admits only known kinds, so probing with an
    # unknown kind is sufficient to verify the keyed read.
    assert load_brief(session, invocation_id=_INVOCATION_ID, brief_kind="not_a_kind") is None
