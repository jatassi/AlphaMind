"""Tests for unevaluable_writer.py (ALP-558).

Covers:
* :func:`write_unevaluable_record` — constructs + inserts an UNEVALUABLE
  record; the inserted record is readable via
  :func:`load_counterfactual_replays_for_envelope` and round-trips correctly.
* Optional ``window_start`` / ``window_end`` populate
  ``replay_data_window_start`` / ``replay_data_window_end``.
* Inserting the same ``(envelope_id, replay_kind)`` twice surfaces an
  ``IntegrityError`` (idempotency-violation surfacing).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import alphamind.state.invocation_context
import alphamind.state.tables  # noqa: F401 — register state tables on metadata
from alphamind._kernel.ids import EnvelopeId
from alphamind.execution.counterfactual_replay_engine.enums import (
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.records import (
    CounterfactualReplayRecord,
)
from alphamind.execution.counterfactual_replay_engine.unevaluable_writer import (
    write_unevaluable_record,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.counterfactual_replays import (
    load_counterfactual_replays_for_envelope,
)

_ENV_1 = EnvelopeId("ENV-REC-10")
_ENV_2 = EnvelopeId("ENV-REC-11")
_WIN_START = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_WIN_END = datetime(2026, 1, 11, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(_engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(_engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteUnevaluableRecord:
    def test_returns_counterfactual_replay_record(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.UNSUPPORTED_INSTRUMENT,
        )
        assert isinstance(result, CounterfactualReplayRecord)

    def test_status_is_unevaluable(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.UNSUPPORTED_INSTRUMENT,
        )
        assert result.replay_status is ReplayStatus.UNEVALUABLE

    def test_reason_is_set(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.DATA_MISSING,
        )
        assert result.unevaluable_reason is UnevaluableReason.DATA_MISSING

    def test_envelope_id_matches(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.DATA_MISSING,
        )
        assert result.pm_decision_envelope_id == _ENV_1

    def test_replay_kind_matches(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.MODIFICATION_ORIGINAL_FORM,
            UnevaluableReason.CORPORATE_ACTION_IN_WINDOW,
        )
        assert result.replay_kind is ReplayKind.MODIFICATION_ORIGINAL_FORM

    def test_replay_timestamp_is_utc(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.UNSUPPORTED_INSTRUMENT,
        )
        assert result.replay_timestamp.tzinfo is not None

    def test_window_always_none_in_unevaluable_record(self, session: Session) -> None:
        """CounterfactualReplayRecord forbids window fields for UNEVALUABLE status.

        The writer accepts window_start/window_end from the call site (so the
        driver can pass the computed window alongside the reason) but does not
        store them in the record — the record invariant enforces None.
        """
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.UNSUPPORTED_INSTRUMENT,
        )
        assert result.replay_data_window_start is None
        assert result.replay_data_window_end is None

    def test_window_params_accepted_without_error(self, session: Session) -> None:
        """Passing window_start/end does not raise even though the record stores None."""
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.DATA_MISSING,
            window_start=_WIN_START,
            window_end=_WIN_END,
        )
        # Record invariant: window fields are always None for UNEVALUABLE
        assert result.replay_data_window_start is None
        assert result.replay_data_window_end is None
        assert result.replay_status is ReplayStatus.UNEVALUABLE

    def test_replay_engine_version_default(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.DATA_MISSING,
        )
        assert result.replay_engine_version == "v2"

    def test_replay_engine_version_custom(self, session: Session) -> None:
        result = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.DATA_MISSING,
            replay_engine_version="v3",
        )
        assert result.replay_engine_version == "v3"


class TestRoundTrip:
    def test_round_trip_via_load(self, session: Session) -> None:
        """Record inserted by write_unevaluable_record is loadable and equal."""
        written = write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.REJECTION,
            UnevaluableReason.DATA_MISSING,
            window_start=_WIN_START,
            window_end=_WIN_END,
        )
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_1)
        assert len(loaded) == 1
        assert loaded[0] == written

    def test_round_trip_no_window(self, session: Session) -> None:
        write_unevaluable_record(
            session,
            _ENV_2,
            ReplayKind.MODIFICATION_ORIGINAL_FORM,
            UnevaluableReason.UNSUPPORTED_INSTRUMENT,
        )
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_2)
        assert len(loaded) == 1
        assert loaded[0].unevaluable_reason is UnevaluableReason.UNSUPPORTED_INSTRUMENT
        assert loaded[0].replay_data_window_start is None

    def test_two_reasons_for_same_envelope_different_kinds(self, session: Session) -> None:
        """REJECTION and MODIFICATION_ORIGINAL_FORM are both allowed per envelope."""
        write_unevaluable_record(
            session, _ENV_1, ReplayKind.REJECTION, UnevaluableReason.DATA_MISSING
        )
        write_unevaluable_record(
            session,
            _ENV_1,
            ReplayKind.MODIFICATION_ORIGINAL_FORM,
            UnevaluableReason.CORPORATE_ACTION_IN_WINDOW,
        )
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_1)
        assert len(loaded) == 2


class TestIdempotencyViolation:
    def test_duplicate_envelope_kind_raises_integrity_error(self, session: Session) -> None:
        """Inserting same (envelope_id, replay_kind) twice surfaces IntegrityError."""
        write_unevaluable_record(
            session, _ENV_1, ReplayKind.REJECTION, UnevaluableReason.DATA_MISSING
        )
        write_unevaluable_record(
            session, _ENV_1, ReplayKind.REJECTION, UnevaluableReason.CORPORATE_ACTION_IN_WINDOW
        )
        with pytest.raises(IntegrityError):
            session.flush()
