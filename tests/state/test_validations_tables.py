"""Tests for the validations + validation_outcomes tables, codecs, and
repository helpers (ALP-874 / story 02b).

Covers:
* ``ValidationRecord`` + ``ValidationsRow`` round-trip codec, including
  ``watched_metric_ids`` as ordered JSON-text.
* ``ValidationOutcomeRecord`` + ``ValidationOutcomesRow`` round-trip codec,
  including ``posterior_summary`` as JSON-text.
* Nullable columns (``registered_by_session_id``, ``evaluated_by_session_id``,
  ``superseded_at``, ``superseded_reason``) survive a real INSERT → SELECT.
* CHECK constraints fire for out-of-vocabulary enum values.
* UNIQUE constraint on ``validation_outcomes.validation_id`` fires on duplicate.
* ``read_pending_validations`` excludes superseded and evaluated validations.
* ``read_outcomes_by_artifact`` returns outcomes ordered most-recent-first.
* ``mark_validation_superseded`` writes the supersession fields.
* ``derive_validation_status`` returns the correct status string.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    OutcomeId,
    RollbackStatus,
    SupersededReason,
    ValidationId,
    ValidationOutcomeRecord,
    ValidationRecord,
    Verdict,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.validation_queries import (
    derive_validation_status,
    insert_validation,
    insert_validation_outcome,
    mark_validation_superseded,
    read_outcomes_by_artifact,
    read_pending_validations,
    read_validation,
    read_validations_superseded_in_window,
)
from alphamind.state.tables.validation_outcomes import ValidationOutcomesRow
from alphamind.state.tables.validations import ValidationsRow

# ---------------------------------------------------------------------------
# Timestamps / IDs used throughout
# ---------------------------------------------------------------------------

_TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_TS2 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)  # evaluation_due_at (7 days later)
_TS_EVAL = datetime(2026, 6, 9, 10, 0, 0, tzinfo=UTC)
_TS_SUPERSEDED = datetime(2026, 6, 5, 9, 0, 0, tzinfo=UTC)

_VAL_ID_1 = ValidationId("val-001")
_VAL_ID_2 = ValidationId("val-002")
_VAL_ID_3 = ValidationId("val-003")
_OUT_ID_1 = OutcomeId("out-001")
_OUT_ID_2 = OutcomeId("out-002")

_METRIC_A = MetricId("win_rate_30d")
_METRIC_B = MetricId("avg_pnl_per_trade")


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_validation(
    validation_id: ValidationId = _VAL_ID_1,
    *,
    edited_artifact: str = "prompts/decision/strategist.md",
    registered_by_session_id: str | None = None,
    superseded_at: datetime | None = None,
    superseded_reason: SupersededReason | None = None,
    registered_at: datetime = _TS,
    watched_metric_ids: tuple[MetricId, ...] = (_METRIC_A, _METRIC_B),
) -> ValidationRecord:
    return ValidationRecord(
        validation_id=validation_id,
        registered_at=registered_at,
        registered_by_session_id=registered_by_session_id,
        edited_artifact=edited_artifact,
        pre_edit_version="abc1234",
        post_edit_version="def5678",
        registered_regime="normal",
        registered_model_id="claude-sonnet-4-5",
        watched_metric_ids=watched_metric_ids,
        window_length_days=7,
        expected_direction=ExpectedDirection.IMPROVED,
        expected_magnitude="5% relative improvement in win rate",
        success_criterion="win_rate_30d increases by ≥5% over baseline",
        failure_criterion="win_rate_30d decreases by ≥3% or stays flat for 14d",
        evaluation_due_at=_TS2,
        superseded_at=superseded_at,
        superseded_reason=superseded_reason,
    )


def _make_outcome(
    outcome_id: OutcomeId = _OUT_ID_1,
    validation_id: ValidationId = _VAL_ID_1,
    *,
    evaluated_by_session_id: str | None = None,
    verdict: Verdict = Verdict.IMPROVED,
    confounder_notes: str | None = None,
    evaluated_at: datetime = _TS_EVAL,
    posterior_summary: dict[str, object] | None = None,
) -> ValidationOutcomeRecord:
    return ValidationOutcomeRecord(
        outcome_id=outcome_id,
        validation_id=validation_id,
        evaluated_at=evaluated_at,
        evaluated_by_session_id=evaluated_by_session_id,
        verdict=verdict,
        posterior_summary=posterior_summary or {"win_rate_30d": {"pre": 0.52, "post": 0.58}},
        confounder_notes=confounder_notes,
        narrative="Win rate improved clearly above the success criterion threshold.",
        rollback_status=RollbackStatus.NOT_APPLICABLE,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
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
# ValidationRecord round-trip
# ---------------------------------------------------------------------------


class TestValidationCodecRoundTrip:
    def test_full_record_survives_round_trip(self, session: Session) -> None:
        """A ``ValidationRecord`` encodes and decodes losslessly."""
        record = _make_validation(
            registered_by_session_id="session-xyz",
            superseded_at=_TS_SUPERSEDED,
            superseded_reason=SupersededReason.REGIME_TRANSITION,
        )
        insert_validation(session, record)
        session.flush()

        loaded = read_validation(session, _VAL_ID_1)
        assert loaded == record

    def test_nullable_fields_round_trip_as_none(self, session: Session) -> None:
        """All three nullable fields survive as None through the codec."""
        record = _make_validation(
            registered_by_session_id=None,
            superseded_at=None,
            superseded_reason=None,
        )
        insert_validation(session, record)
        session.flush()

        loaded = read_validation(session, _VAL_ID_1)
        assert loaded is not None
        assert loaded.registered_by_session_id is None
        assert loaded.superseded_at is None
        assert loaded.superseded_reason is None

    def test_watched_metric_ids_order_preserved(self, session: Session) -> None:
        """``watched_metric_ids`` preserves insertion order through JSON-text."""
        ordered = (MetricId("z_metric"), MetricId("a_metric"), MetricId("m_metric"))
        record = _make_validation(watched_metric_ids=ordered)
        insert_validation(session, record)
        session.flush()

        loaded = read_validation(session, _VAL_ID_1)
        assert loaded is not None
        assert loaded.watched_metric_ids == ordered

    def test_read_nonexistent_returns_none(self, session: Session) -> None:
        result = read_validation(session, ValidationId("no-such-id"))
        assert result is None


# ---------------------------------------------------------------------------
# ValidationOutcomeRecord round-trip
# ---------------------------------------------------------------------------


class TestValidationOutcomeCodecRoundTrip:
    def test_full_outcome_survives_round_trip(self, session: Session) -> None:
        """A ``ValidationOutcomeRecord`` encodes and decodes losslessly."""
        validation = _make_validation()
        insert_validation(session, validation)
        session.flush()

        posterior = {
            "win_rate_30d": {"pre": 0.52, "post": 0.58, "uncertainty": "low"},
            "avg_pnl_per_trade": {"pre": 120.5, "post": 145.3},
        }
        outcome = _make_outcome(
            evaluated_by_session_id="session-abc",
            confounder_notes="Minor regime shift mid-window.",
            posterior_summary=posterior,
        )
        insert_validation_outcome(session, outcome)
        session.flush()

        # Re-fetch via a raw query to avoid session identity map short-circuit.
        session.expire_all()
        from alphamind.state.repository.validation_queries import (
            read_outcomes_by_artifact,
        )

        loaded = read_outcomes_by_artifact(session, "prompts/decision/strategist.md")
        assert len(loaded) == 1
        assert loaded[0] == outcome

    def test_nullable_outcome_fields_round_trip_as_none(self, session: Session) -> None:
        """``evaluated_by_session_id`` and ``confounder_notes`` survive as None."""
        insert_validation(session, _make_validation())
        session.flush()

        outcome = _make_outcome(
            evaluated_by_session_id=None,
            confounder_notes=None,
        )
        insert_validation_outcome(session, outcome)
        session.flush()

        results = read_outcomes_by_artifact(session, "prompts/decision/strategist.md")
        assert len(results) == 1
        assert results[0].evaluated_by_session_id is None
        assert results[0].confounder_notes is None

    def test_posterior_summary_nested_structure_preserved(self, session: Session) -> None:
        """Nested dict structure in ``posterior_summary`` survives JSON-text round-trip."""
        insert_validation(session, _make_validation())
        session.flush()

        nested: dict[str, object] = {
            "metrics": [
                {"id": "win_rate_30d", "delta": 0.06, "significant": True},
            ],
            "method": "bayesian",
        }
        outcome = _make_outcome(posterior_summary=nested)
        insert_validation_outcome(session, outcome)
        session.flush()

        results = read_outcomes_by_artifact(session, "prompts/decision/strategist.md")
        assert results[0].posterior_summary == nested


# ---------------------------------------------------------------------------
# CHECK constraint enforcement
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    def test_bad_expected_direction_rejected(self, session: Session) -> None:
        """INSERT with an invalid ``expected_direction`` value raises IntegrityError."""
        row = ValidationsRow(
            validation_id="val-bad-dir",
            registered_at=_TS.isoformat(),
            edited_artifact="foo.md",
            pre_edit_version="aaa",
            post_edit_version="bbb",
            registered_regime="normal",
            registered_model_id="claude-sonnet-4-5",
            watched_metric_ids_json="[]",
            window_length_days=7,
            expected_direction="sideways",  # invalid
            expected_magnitude="some amount",
            success_criterion="something",
            failure_criterion="other thing",
            evaluation_due_at=_TS2.isoformat(),
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.flush()

    def test_bad_superseded_reason_rejected(self, session: Session) -> None:
        """INSERT with an invalid ``superseded_reason`` value raises IntegrityError."""
        row = ValidationsRow(
            validation_id="val-bad-reason",
            registered_at=_TS.isoformat(),
            edited_artifact="foo.md",
            pre_edit_version="aaa",
            post_edit_version="bbb",
            registered_regime="normal",
            registered_model_id="claude-sonnet-4-5",
            watched_metric_ids_json="[]",
            window_length_days=7,
            expected_direction=ExpectedDirection.IMPROVED.value,
            expected_magnitude="some amount",
            success_criterion="something",
            failure_criterion="other thing",
            evaluation_due_at=_TS2.isoformat(),
            superseded_at=_TS.isoformat(),
            superseded_reason="bad_reason",  # invalid
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.flush()

    def test_bad_verdict_rejected(self, session: Session) -> None:
        """INSERT with an invalid ``verdict`` value raises IntegrityError."""
        insert_validation(session, _make_validation())
        session.flush()

        row = ValidationOutcomesRow(
            outcome_id="out-bad-verdict",
            validation_id=str(_VAL_ID_1),
            evaluated_at=_TS_EVAL.isoformat(),
            verdict="ambiguous",  # invalid
            posterior_summary_json="{}",
            narrative="some narrative",
            rollback_status=RollbackStatus.NOT_APPLICABLE.value,
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.flush()

    def test_bad_rollback_status_rejected(self, session: Session) -> None:
        """INSERT with an invalid ``rollback_status`` value raises IntegrityError."""
        insert_validation(session, _make_validation())
        session.flush()

        row = ValidationOutcomesRow(
            outcome_id="out-bad-rollback",
            validation_id=str(_VAL_ID_1),
            evaluated_at=_TS_EVAL.isoformat(),
            verdict=Verdict.NO_CHANGE.value,
            posterior_summary_json="{}",
            narrative="some narrative",
            rollback_status="rollback_now",  # invalid
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.flush()


# ---------------------------------------------------------------------------
# UNIQUE constraint on validation_outcomes.validation_id
# ---------------------------------------------------------------------------


class TestUniqueOutcomeConstraint:
    def test_second_outcome_for_same_validation_raises(self, session: Session) -> None:
        """The UNIQUE constraint enforces one outcome per validation."""
        insert_validation(session, _make_validation())
        session.flush()

        insert_validation_outcome(session, _make_outcome(_OUT_ID_1))
        session.flush()

        insert_validation_outcome(
            session,
            _make_outcome(_OUT_ID_2),  # different outcome_id, same validation_id
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_outcomes_for_different_validations_both_accepted(self, session: Session) -> None:
        """Two outcomes for two distinct validations are both valid."""
        insert_validation(session, _make_validation(_VAL_ID_1))
        insert_validation(session, _make_validation(_VAL_ID_2))
        session.flush()

        insert_validation_outcome(session, _make_outcome(_OUT_ID_1, _VAL_ID_1))
        insert_validation_outcome(session, _make_outcome(_OUT_ID_2, _VAL_ID_2))
        session.flush()  # must not raise


# ---------------------------------------------------------------------------
# read_pending_validations
# ---------------------------------------------------------------------------


class TestReadPendingValidations:
    def test_returns_only_pending(self, session: Session) -> None:
        """``read_pending_validations`` returns only non-superseded, non-evaluated rows."""
        insert_validation(session, _make_validation(_VAL_ID_1))  # pending
        insert_validation(  # superseded
            session,
            _make_validation(
                _VAL_ID_2,
                superseded_at=_TS_SUPERSEDED,
                superseded_reason=SupersededReason.MODEL_VERSION_CHANGE,
            ),
        )
        insert_validation(session, _make_validation(_VAL_ID_3))  # evaluated
        session.flush()

        insert_validation_outcome(session, _make_outcome(_OUT_ID_1, _VAL_ID_3))
        session.flush()

        pending = read_pending_validations(session)
        pending_ids = {r.validation_id for r in pending}
        assert pending_ids == {_VAL_ID_1}

    def test_empty_when_all_superseded_or_evaluated(self, session: Session) -> None:
        insert_validation(
            session,
            _make_validation(
                _VAL_ID_1,
                superseded_at=_TS_SUPERSEDED,
                superseded_reason=SupersededReason.REGIME_TRANSITION,
            ),
        )
        insert_validation(session, _make_validation(_VAL_ID_2))
        session.flush()

        insert_validation_outcome(session, _make_outcome(_OUT_ID_1, _VAL_ID_2))
        session.flush()

        assert read_pending_validations(session) == ()

    def test_returns_empty_when_no_validations(self, session: Session) -> None:
        assert read_pending_validations(session) == ()


# ---------------------------------------------------------------------------
# read_validations_superseded_in_window
# ---------------------------------------------------------------------------


class TestReadValidationsSupersededInWindow:
    _WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
    _WINDOW_END = datetime(2026, 6, 8, tzinfo=UTC)

    def test_returns_only_supersessions_in_window(self, session: Session) -> None:
        """Only validations whose ``superseded_at`` falls in ``[start, end)`` return."""
        insert_validation(  # superseded inside the window
            session,
            _make_validation(
                _VAL_ID_1,
                superseded_at=datetime(2026, 6, 5, 9, 0, tzinfo=UTC),
                superseded_reason=SupersededReason.REGIME_TRANSITION,
            ),
        )
        insert_validation(  # superseded before the window
            session,
            _make_validation(
                _VAL_ID_2,
                superseded_at=datetime(2026, 5, 20, 9, 0, tzinfo=UTC),
                superseded_reason=SupersededReason.MODEL_VERSION_CHANGE,
            ),
        )
        insert_validation(session, _make_validation(_VAL_ID_3))  # never superseded
        session.flush()

        result = read_validations_superseded_in_window(
            session, self._WINDOW_START, self._WINDOW_END
        )
        assert {r.validation_id for r in result} == {_VAL_ID_1}
        assert result[0].superseded_reason is SupersededReason.REGIME_TRANSITION

    def test_end_is_exclusive(self, session: Session) -> None:
        """A supersession exactly at ``end`` is excluded (end-exclusive)."""
        insert_validation(
            session,
            _make_validation(
                _VAL_ID_1,
                superseded_at=self._WINDOW_END,
                superseded_reason=SupersededReason.REGIME_TRANSITION,
            ),
        )
        session.flush()
        assert (
            read_validations_superseded_in_window(session, self._WINDOW_START, self._WINDOW_END)
            == ()
        )

    def test_start_is_inclusive(self, session: Session) -> None:
        """A supersession exactly at ``start`` is included (start-inclusive)."""
        insert_validation(
            session,
            _make_validation(
                _VAL_ID_1,
                superseded_at=self._WINDOW_START,
                superseded_reason=SupersededReason.CONCURRENT_EDIT_ON_WATCHED_ARTIFACT,
            ),
        )
        session.flush()
        result = read_validations_superseded_in_window(
            session, self._WINDOW_START, self._WINDOW_END
        )
        assert {r.validation_id for r in result} == {_VAL_ID_1}

    def test_empty_when_no_supersessions(self, session: Session) -> None:
        insert_validation(session, _make_validation(_VAL_ID_1))
        session.flush()
        assert (
            read_validations_superseded_in_window(session, self._WINDOW_START, self._WINDOW_END)
            == ()
        )


# ---------------------------------------------------------------------------
# read_outcomes_by_artifact
# ---------------------------------------------------------------------------


class TestReadOutcomesByArtifact:
    def test_returns_outcomes_for_requested_artifact_only(self, session: Session) -> None:
        """Outcomes for a different artifact are excluded."""
        insert_validation(
            session,
            _make_validation(_VAL_ID_1, edited_artifact="prompts/decision/strategist.md"),
        )
        insert_validation(
            session,
            _make_validation(_VAL_ID_2, edited_artifact="prompts/analysis/analyst.md"),
        )
        session.flush()

        insert_validation_outcome(session, _make_outcome(_OUT_ID_1, _VAL_ID_1))
        insert_validation_outcome(session, _make_outcome(_OUT_ID_2, _VAL_ID_2))
        session.flush()

        results = read_outcomes_by_artifact(session, "prompts/decision/strategist.md")
        assert len(results) == 1
        assert results[0].outcome_id == _OUT_ID_1

    def test_most_recent_first_ordering(self, session: Session) -> None:
        """Results are ordered by ``evaluated_at`` descending."""
        val_id_4 = ValidationId("val-004")
        out_id_3 = OutcomeId("out-003")

        insert_validation(
            session,
            _make_validation(
                _VAL_ID_1,
                registered_at=_TS,
                edited_artifact="prompts/decision/strategist.md",
            ),
        )
        insert_validation(
            session,
            _make_validation(
                val_id_4,
                registered_at=_TS + timedelta(days=1),
                edited_artifact="prompts/decision/strategist.md",
            ),
        )
        session.flush()

        earlier_eval = _TS_EVAL
        later_eval = _TS_EVAL + timedelta(hours=3)

        insert_validation_outcome(
            session,
            _make_outcome(_OUT_ID_1, _VAL_ID_1, evaluated_at=earlier_eval),
        )
        insert_validation_outcome(
            session,
            _make_outcome(out_id_3, val_id_4, evaluated_at=later_eval),
        )
        session.flush()

        results = read_outcomes_by_artifact(session, "prompts/decision/strategist.md")
        assert len(results) == 2
        assert results[0].outcome_id == out_id_3
        assert results[1].outcome_id == _OUT_ID_1

    def test_returns_empty_for_unknown_artifact(self, session: Session) -> None:
        results = read_outcomes_by_artifact(session, "no/such/artifact.md")
        assert results == ()


# ---------------------------------------------------------------------------
# mark_validation_superseded
# ---------------------------------------------------------------------------


class TestMarkValidationSuperseded:
    def test_supersedes_pending_validation(self, session: Session) -> None:
        """``mark_validation_superseded`` writes both supersession fields."""
        insert_validation(session, _make_validation())
        session.flush()

        mark_validation_superseded(
            session,
            _VAL_ID_1,
            SupersededReason.CONCURRENT_EDIT_ON_WATCHED_ARTIFACT,
            _TS_SUPERSEDED,
        )
        session.flush()

        loaded = read_validation(session, _VAL_ID_1)
        assert loaded is not None
        assert loaded.superseded_at == _TS_SUPERSEDED
        assert loaded.superseded_reason == SupersededReason.CONCURRENT_EDIT_ON_WATCHED_ARTIFACT

    def test_raises_for_unknown_id(self, session: Session) -> None:
        """Superseding a non-existent validation raises ``ValueError``."""
        with pytest.raises(ValueError, match="no validation row found"):
            mark_validation_superseded(
                session,
                ValidationId("ghost-id"),
                SupersededReason.REGIME_TRANSITION,
                _TS_SUPERSEDED,
            )


# ---------------------------------------------------------------------------
# derive_validation_status
# ---------------------------------------------------------------------------


class TestDeriveValidationStatus:
    def test_pending_when_no_outcome_not_superseded(self, session: Session) -> None:
        insert_validation(session, _make_validation())
        session.flush()

        row = session.get(ValidationsRow, str(_VAL_ID_1))
        assert row is not None
        assert derive_validation_status(row, has_outcome=False) == "pending"

    def test_superseded_when_superseded_at_set(self, session: Session) -> None:
        insert_validation(
            session,
            _make_validation(
                superseded_at=_TS_SUPERSEDED,
                superseded_reason=SupersededReason.REGIME_TRANSITION,
            ),
        )
        session.flush()

        row = session.get(ValidationsRow, str(_VAL_ID_1))
        assert row is not None
        # superseded takes priority even if (hypothetically) has_outcome=True
        assert derive_validation_status(row, has_outcome=True) == "superseded"

    def test_evaluated_when_outcome_exists(self, session: Session) -> None:
        insert_validation(session, _make_validation())
        session.flush()

        row = session.get(ValidationsRow, str(_VAL_ID_1))
        assert row is not None
        assert derive_validation_status(row, has_outcome=True) == "evaluated"
