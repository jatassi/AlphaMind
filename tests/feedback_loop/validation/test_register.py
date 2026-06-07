"""REGISTER shell — provenance snapshot + criteria freeze (ALP-889 story 07b).

``register_validation`` snapshots ``registered_regime`` (from the registering
invocation's ``active_regime``) and ``registered_model_id`` (from the
invocation's ``agent_calls`` provenance), computes
``evaluation_due_at = registered_at + window_length_days``, freezes the
success/failure criteria + watched-metric ids, and persists the validation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.metrics.types import MetricId
from alphamind.feedback_loop.validation.records import ExpectedDirection
from alphamind.feedback_loop.validation.register import (
    ProvenanceLookupError,
    register_validation,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.validation_queries import read_validation
from alphamind.state.tables.agent_calls import AgentCallsRow
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_PLT = "plt-register"
_INV = "inv-register"
_REGISTERED_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _agent_call_row(call_id: str, *, model_id: str) -> AgentCallsRow:
    return AgentCallsRow(
        agent_call_id=call_id,
        invocation_id=_INV,
        agent_name="strategist",
        attempt_number=1,
        model_id=model_id,
        prompt_path="prompts/decision/strategist.md",
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


@pytest.fixture()
def session(tmp_path: Path) -> Iterator[Session]:
    engine = make_engine(str(tmp_path / "register.db"))
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        inv = stub_invocation_row(_INV, process_lifetime_id=_PLT)
        inv.active_regime = "elevated"
        sess.add(inv)
        sess.flush()
        sess.add(_agent_call_row("call-1", model_id="claude-opus-4-8"))
        sess.commit()
        yield sess
    engine.dispose()


def _register(session: Session, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "validation_id": "val-1",
        "registering_invocation_id": _INV,
        "registered_at": _REGISTERED_AT,
        "edited_artifact": "prompts/decision/strategist.md",
        "pre_edit_version": "abc1234",
        "post_edit_version": "def5678",
        "watched_metric_ids": (MetricId("pm_approval_rate"),),
        "window_length_days": 21,
        "expected_direction": ExpectedDirection.IMPROVED,
        "expected_magnitude": "PM rejection rate down 5-10pp",
        "success_criterion": "rejection rate below 30%",
        "failure_criterion": "rejection rate stays above 35%",
    }
    kwargs.update(overrides)
    return register_validation(session, **kwargs)  # type: ignore[arg-type]


class TestRegisterValidation:
    def test_snapshots_regime_from_invocation(self, session: Session) -> None:
        vid = _register(session)
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.registered_regime == "elevated"

    def test_snapshots_model_id_from_agent_calls(self, session: Session) -> None:
        vid = _register(session)
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.registered_model_id == "claude-opus-4-8"

    def test_evaluation_due_at_is_registered_at_plus_window(self, session: Session) -> None:
        vid = _register(session)
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.evaluation_due_at == _REGISTERED_AT + timedelta(days=21)

    def test_freezes_criteria_and_watched_metrics(self, session: Session) -> None:
        vid = _register(session)
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.success_criterion == "rejection rate below 30%"
        assert record.failure_criterion == "rejection rate stays above 35%"
        assert record.watched_metric_ids == (MetricId("pm_approval_rate"),)

    def test_session_id_optional_and_nullable(self, session: Session) -> None:
        vid = _register(session)
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.registered_by_session_id is None

    def test_session_id_persisted_when_supplied(self, session: Session) -> None:
        vid = _register(session, session_id="sess-42")
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.registered_by_session_id == "sess-42"

    def test_not_superseded_on_registration(self, session: Session) -> None:
        vid = _register(session)
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.superseded_at is None
        assert record.superseded_reason is None

    def test_seeded_provenance_bypasses_lookup(self, session: Session) -> None:
        """The paired post-rollback path supplies regime + model_id directly,
        so registration does not depend on a live registering invocation."""
        vid = register_validation(
            session,
            validation_id="val-seeded",
            registering_invocation_id=None,
            registered_at=_REGISTERED_AT,
            edited_artifact="prompts/decision/strategist.md",
            pre_edit_version="def5678",
            post_edit_version="ghi9012",
            watched_metric_ids=(MetricId("pm_approval_rate"),),
            window_length_days=21,
            expected_direction=ExpectedDirection.IMPROVED,
            expected_magnitude="restore to baseline",
            success_criterion="metric returns to baseline band",
            failure_criterion="metric stays at failed-edit value",
            registered_regime="crisis",
            registered_model_id="claude-sonnet-4-5",
        )
        session.commit()
        record = read_validation(session, vid)  # type: ignore[arg-type]
        assert record is not None
        assert record.registered_regime == "crisis"
        assert record.registered_model_id == "claude-sonnet-4-5"

    def test_missing_invocation_raises(self, session: Session) -> None:
        with pytest.raises(ProvenanceLookupError):
            _register(session, registering_invocation_id="inv-does-not-exist")
