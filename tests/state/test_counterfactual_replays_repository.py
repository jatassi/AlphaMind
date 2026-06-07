"""Tests for the counterfactual_replays repository helpers (ALP-556).

Covers:
* ``insert_counterfactual_replay`` — encodes + inserts; idempotency is at the
  engine level (the table raises on duplicate); confirms the row is readable.
* ``load_counterfactual_replays_for_envelope`` — returns all rows for an
  envelope, ordered by replay_kind.
* Unique-constraint violation (same envelope + kind) raises IntegrityError.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import alphamind.state.invocation_context  # noqa: F401 — break circular import seam
from alphamind._kernel.ids import EnvelopeId, ReplayId
from alphamind._kernel.money import Money, money
from alphamind.execution.counterfactual_replay_engine.enums import (
    Confidence,
    ExitLeg,
    ReplayKind,
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.records import (
    CounterfactualReplayRecord,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.counterfactual_replays import (
    insert_counterfactual_replay,
    load_counterfactual_replays_for_envelope,
)

_TS = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
_TS2 = datetime(2026, 6, 6, 13, 0, 0, tzinfo=UTC)
_TS3 = datetime(2026, 6, 6, 14, 0, 0, tzinfo=UTC)

_ENV_1 = EnvelopeId("ENV-REC-1")
_ENV_2 = EnvelopeId("ENV-REC-2")


def _evaluated_record(
    replay_id: str,
    envelope_id: EnvelopeId,
    kind: ReplayKind = ReplayKind.REJECTION,
) -> CounterfactualReplayRecord:
    return CounterfactualReplayRecord(
        replay_id=ReplayId(replay_id),
        pm_decision_envelope_id=envelope_id,
        replay_kind=kind,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=True,
        entry_price=money(Decimal("100.00")),
        entry_timestamp=_TS,
        entry_slippage=money(Decimal("0.05")),
        entry_fees=money(Decimal("0.10")),
        exit_leg=ExitLeg.TARGET_HIT,
        exit_price=money(Decimal("110.00")),
        exit_timestamp=_TS2,
        exit_slippage=money(Decimal("0.08")),
        exit_fees=money(Decimal("0.12")),
        realized_pl=Money(Decimal("9.15")),
        confidence=Confidence.HIGH,
        replay_timestamp=_TS3,
        replay_data_window_start=_TS,
        replay_data_window_end=_TS2,
        replay_engine_version="v2.0.0",
    )


def _unevaluable_record(
    replay_id: str,
    envelope_id: EnvelopeId,
    kind: ReplayKind = ReplayKind.REJECTION,
) -> CounterfactualReplayRecord:
    return CounterfactualReplayRecord(
        replay_id=ReplayId(replay_id),
        pm_decision_envelope_id=envelope_id,
        replay_kind=kind,
        replay_status=ReplayStatus.UNEVALUABLE,
        unevaluable_reason=UnevaluableReason.DATA_MISSING,
        entered=None,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=None,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        confidence=None,
        replay_timestamp=_TS3,
        replay_data_window_start=None,
        replay_data_window_end=None,
        replay_engine_version="v2.0.0",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import alphamind.state.tables  # noqa: F401 — register all tables

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
# Tests
# ---------------------------------------------------------------------------


class TestInsertCounterfactualReplay:
    def test_insert_then_load_returns_same_record(self, session: Session) -> None:
        record = _evaluated_record("rpl-1", _ENV_1)
        insert_counterfactual_replay(session, record)
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_1)
        assert len(loaded) == 1
        assert loaded[0] == record

    def test_insert_unevaluable_record(self, session: Session) -> None:
        record = _unevaluable_record("rpl-2", _ENV_1)
        insert_counterfactual_replay(session, record)
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_1)
        assert len(loaded) == 1
        assert loaded[0] == record

    def test_insert_two_kinds_for_same_envelope(self, session: Session) -> None:
        """Both rejection and modification_original_form are allowed per envelope."""
        r1 = _evaluated_record("rpl-3a", _ENV_1, ReplayKind.REJECTION)
        r2 = _unevaluable_record("rpl-3b", _ENV_1, ReplayKind.MODIFICATION_ORIGINAL_FORM)
        insert_counterfactual_replay(session, r1)
        insert_counterfactual_replay(session, r2)
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_1)
        assert len(loaded) == 2

    def test_load_returns_records_for_requested_envelope_only(self, session: Session) -> None:
        r_env1 = _evaluated_record("rpl-4a", _ENV_1)
        r_env2 = _evaluated_record("rpl-4b", _ENV_2)
        insert_counterfactual_replay(session, r_env1)
        insert_counterfactual_replay(session, r_env2)
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_1)
        assert len(loaded) == 1
        assert loaded[0].pm_decision_envelope_id == _ENV_1

    def test_load_empty_envelope_returns_empty_tuple(self, session: Session) -> None:
        loaded = load_counterfactual_replays_for_envelope(session, EnvelopeId("ENV-REC-999"))
        assert loaded == ()

    def test_load_ordered_by_replay_kind(self, session: Session) -> None:
        """Results are ordered by replay_kind (lexicographic on the stored value)."""
        r_mod = _unevaluable_record("rpl-5a", _ENV_1, ReplayKind.MODIFICATION_ORIGINAL_FORM)
        r_rej = _evaluated_record("rpl-5b", _ENV_1, ReplayKind.REJECTION)
        insert_counterfactual_replay(session, r_rej)
        insert_counterfactual_replay(session, r_mod)
        session.flush()

        loaded = load_counterfactual_replays_for_envelope(session, _ENV_1)
        assert len(loaded) == 2
        # "modification_original_form" < "rejection" lexicographically
        assert loaded[0].replay_kind == ReplayKind.MODIFICATION_ORIGINAL_FORM
        assert loaded[1].replay_kind == ReplayKind.REJECTION


class TestUniqueConstraintViolation:
    def test_duplicate_envelope_kind_raises_integrity_error(self, session: Session) -> None:
        """Two inserts with the same (envelope, kind) raise IntegrityError.

        The UniqueConstraint ``uq_counterfactual_replays_envelope_kind`` enforces
        one-record-per-(envelope, replay_kind). Idempotency at the engine layer
        is the caller's responsibility; the table fails closed.
        """
        r1 = _evaluated_record("rpl-6a", _ENV_1, ReplayKind.REJECTION)
        r2 = _unevaluable_record("rpl-6b", _ENV_1, ReplayKind.REJECTION)
        insert_counterfactual_replay(session, r1)
        session.flush()
        insert_counterfactual_replay(session, r2)
        with pytest.raises(IntegrityError):
            session.flush()
