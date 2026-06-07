"""Supersession detector — regime / model / concurrent-edit triggers (ALP-892).

The detector marks an active validation ``superseded`` when its post-edit window
crosses a structural conditioning shift the registration was a contract over. Each
trigger is exercised in isolation against an on-disk SQLite DB seeded with a
registering invocation plus post-window invocations / agent calls; the
concurrent-edit trigger runs against a throwaway temp git repo (never the live one).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.validation import cli
from alphamind.feedback_loop.validation.records import SupersededReason
from alphamind.feedback_loop.validation.supersession import detect_supersessions
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.validation_queries import read_validation
from alphamind.state.tables.agent_calls import AgentCallRecord, AgentCallsRow
from alphamind.state.tables.agent_calls_codec import record_to_row
from alphamind.state.tables.validations import ValidationsRow
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_PLT = "plt-sup"
_REG_INV = "inv-reg"
_REGISTERED_AT = datetime(2026, 6, 1, tzinfo=UTC)
_WINDOW_DAYS = 21
_DUE_AT = _REGISTERED_AT + timedelta(days=_WINDOW_DAYS)
_ARTIFACT = "prompts/decision/strategist.md"
_REG_MODEL = "claude-opus-4-8"
_REG_REGIME = "normal"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent_call(call_id: str, invocation_id: str, *, model_id: str = _REG_MODEL) -> AgentCallRecord:
    return AgentCallRecord(
        agent_call_id=call_id,
        invocation_id=invocation_id,
        agent_name="strategist",
        attempt_number=1,
        model_id=model_id,
        prompt_path=_ARTIFACT,
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json="{}",
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        wall_clock_ms=1,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=None,
    )


def _validation_row(
    validation_id: str,
    *,
    superseded_at: str | None = None,
    superseded_reason: str | None = None,
    edited_artifact: str = _ARTIFACT,
) -> ValidationsRow:
    return ValidationsRow(
        validation_id=validation_id,
        registered_at=_iso(_REGISTERED_AT),
        registered_by_session_id=None,
        edited_artifact=edited_artifact,
        pre_edit_version="abc1234",
        post_edit_version="def5678",
        registered_regime=_REG_REGIME,
        registered_model_id=_REG_MODEL,
        watched_metric_ids_json=json.dumps(["m1"]),
        window_length_days=_WINDOW_DAYS,
        expected_direction="improved",
        expected_magnitude="more",
        success_criterion="up",
        failure_criterion="down",
        evaluation_due_at=_iso(_DUE_AT),
        superseded_at=superseded_at,
        superseded_reason=superseded_reason,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """An on-disk DB with the process-lifetime parent + registering invocation."""
    path = str(tmp_path / "sup.db")
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        reg = stub_invocation_row(_REG_INV, process_lifetime_id=_PLT)
        reg.start_at = _iso(_REGISTERED_AT)
        reg.active_regime = _REG_REGIME
        sess.add(reg)
        sess.flush()
        sess.add(AgentCallsRow(**record_to_row(_agent_call("call-reg", _REG_INV))))
        sess.commit()
    engine.dispose()
    return path


def _add_invocation(
    db_path: str,
    invocation_id: str,
    *,
    start_at: datetime,
    regime: str = _REG_REGIME,
) -> None:
    engine = make_engine(db_path)
    with make_session_factory(engine)() as sess:
        inv = stub_invocation_row(invocation_id, process_lifetime_id=_PLT)
        inv.start_at = _iso(start_at)
        inv.active_regime = regime
        sess.add(inv)
        sess.commit()
    engine.dispose()


def _add_agent_call(
    db_path: str,
    call_id: str,
    invocation_id: str,
    *,
    model_id: str,
) -> None:
    engine = make_engine(db_path)
    with make_session_factory(engine)() as sess:
        sess.add(AgentCallsRow(**record_to_row(_agent_call(call_id, invocation_id, model_id=model_id))))
        sess.commit()
    engine.dispose()


def _add_validation(db_path: str, row: ValidationsRow) -> None:
    engine = make_engine(db_path)
    with make_session_factory(engine)() as sess:
        sess.add(row)
        sess.commit()
    engine.dispose()


def _read(db_path: str, validation_id: str) -> object:
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as sess:
            from alphamind.feedback_loop.validation.records import ValidationId

            return read_validation(sess, ValidationId(validation_id))
    finally:
        engine.dispose()


def _run_detect(db_path: str) -> int:
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as sess:
            marked = detect_supersessions(sess)
            sess.commit()
    finally:
        engine.dispose()
    return marked


class TestRegimeTransition:
    def test_post_window_regime_shift_supersedes(self, db_path: str) -> None:
        _add_validation(db_path, _validation_row("val-regime"))
        # A post-window invocation classified into a different regime.
        _add_invocation(
            db_path,
            "inv-post",
            start_at=_REGISTERED_AT + timedelta(days=3),
            regime="defensive",
        )
        marked = _run_detect(db_path)
        assert marked == 1
        record = _read(db_path, "val-regime")
        assert record is not None
        assert record.superseded_at is not None  # type: ignore[attr-defined]
        assert record.superseded_reason is SupersededReason.REGIME_TRANSITION  # type: ignore[attr-defined]
