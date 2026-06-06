"""Tests for CounterfactualReplays table + codec (ALP-556).

Covers:
* Column shape, CHECK constraints, UniqueConstraint, indexes
* Codec round-trips: encode/decode for all valid record permutations
  - evaluated-equity (entered=True, all exit fields)
  - evaluated-option (entered=True, STRATEGIST_CLOSE_AT_PROPOSAL exit)
  - unevaluable
  - entered-False (ENTRY_WINDOW_EXPIRED_UNFILLED)
  - strategist-close-at-proposal shape
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import EnvelopeId, ReplayId
from alphamind._kernel.money import Money, money, price
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
from alphamind.state.tables.counterfactual_replays import CounterfactualReplays
from alphamind.state.tables.counterfactual_replays_codec import (
    decode_counterfactual_replay,
    encode_counterfactual_replay,
)

_TS = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
_TS2 = datetime(2026, 6, 6, 13, 0, 0, tzinfo=UTC)
_TS3 = datetime(2026, 6, 6, 14, 0, 0, tzinfo=UTC)


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
# Helpers: canonical record shapes
# ---------------------------------------------------------------------------


def _evaluated_equity_record(
    replay_id: str = "rpl-1",
    envelope_id: str = "ENV-REC-1",
    kind: ReplayKind = ReplayKind.REJECTION,
) -> CounterfactualReplayRecord:
    """Evaluated, entered=True, full exit fields, TARGET_HIT."""
    return CounterfactualReplayRecord(
        replay_id=ReplayId(replay_id),
        pm_decision_envelope_id=EnvelopeId(envelope_id),
        replay_kind=kind,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=True,
        entry_price=price(Decimal("100.50")),
        entry_timestamp=_TS,
        entry_slippage=money(Decimal("0.05")),
        entry_fees=money(Decimal("0.10")),
        exit_leg=ExitLeg.TARGET_HIT,
        exit_price=price(Decimal("110.00")),
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


def _evaluated_strategist_close_record(
    replay_id: str = "rpl-2",
    envelope_id: str = "ENV-SA-1",
) -> CounterfactualReplayRecord:
    """Evaluated, entered=True, STRATEGIST_CLOSE_AT_PROPOSAL exit."""
    return CounterfactualReplayRecord(
        replay_id=ReplayId(replay_id),
        pm_decision_envelope_id=EnvelopeId(envelope_id),
        replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=True,
        entry_price=price(Decimal("200.00")),
        entry_timestamp=_TS,
        entry_slippage=money(Decimal("0.15")),
        entry_fees=money(Decimal("0.20")),
        exit_leg=ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL,
        exit_price=price(Decimal("195.00")),
        exit_timestamp=_TS2,
        exit_slippage=money(Decimal("0.12")),
        exit_fees=money(Decimal("0.18")),
        realized_pl=Money(Decimal("-5.65")),
        confidence=Confidence.MEDIUM,
        replay_timestamp=_TS3,
        replay_data_window_start=_TS,
        replay_data_window_end=_TS2,
        replay_engine_version="v2.0.0",
    )


def _unevaluable_record(
    replay_id: str = "rpl-3",
    envelope_id: str = "ENV-REC-3",
    reason: UnevaluableReason = UnevaluableReason.DATA_MISSING,
) -> CounterfactualReplayRecord:
    """Unevaluable — all simulation fields None."""
    return CounterfactualReplayRecord(
        replay_id=ReplayId(replay_id),
        pm_decision_envelope_id=EnvelopeId(envelope_id),
        replay_kind=ReplayKind.REJECTION,
        replay_status=ReplayStatus.UNEVALUABLE,
        unevaluable_reason=reason,
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


def _entered_false_record(
    replay_id: str = "rpl-4",
    envelope_id: str = "ENV-REC-4",
) -> CounterfactualReplayRecord:
    """Evaluated, entered=False — only ENTRY_WINDOW_EXPIRED_UNFILLED exit, no exit fields."""
    return CounterfactualReplayRecord(
        replay_id=ReplayId(replay_id),
        pm_decision_envelope_id=EnvelopeId(envelope_id),
        replay_kind=ReplayKind.REJECTION,
        replay_status=ReplayStatus.EVALUATED,
        unevaluable_reason=None,
        entered=False,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        confidence=Confidence.LOW,
        replay_timestamp=_TS3,
        replay_data_window_start=_TS,
        replay_data_window_end=None,
        replay_engine_version="v2.0.0",
    )


# ---------------------------------------------------------------------------
# Slice 1: Table shape
# ---------------------------------------------------------------------------


class TestCounterfactualReplaysTableShape:
    def test_table_exists_after_create_all(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        assert "counterfactual_replays" in tables

    def test_required_columns_present(self, engine: Engine) -> None:
        col_names = {c["name"] for c in inspect(engine).get_columns("counterfactual_replays")}
        expected = {
            "replay_id",
            "pm_decision_envelope_id",
            "replay_kind",
            "replay_status",
            "unevaluable_reason",
            "entered",
            "entry_price",
            "entry_timestamp",
            "entry_slippage",
            "entry_fees",
            "exit_leg",
            "exit_price",
            "exit_timestamp",
            "exit_slippage",
            "exit_fees",
            "realized_pl",
            "confidence",
            "replay_timestamp",
            "replay_data_window_start",
            "replay_data_window_end",
            "replay_engine_version",
        }
        assert expected <= col_names

    def test_indexes_present(self, engine: Engine) -> None:
        idx_names = {i["name"] for i in inspect(engine).get_indexes("counterfactual_replays")}
        assert "ix_counterfactual_replays_envelope" in idx_names
        assert "ix_counterfactual_replays_status_timestamp" in idx_names

    def test_unique_constraint_envelope_kind(self, session: Session) -> None:
        row1 = encode_counterfactual_replay(_evaluated_equity_record())
        row2 = encode_counterfactual_replay(_evaluated_equity_record(replay_id="rpl-1b"))
        # Both have same envelope_id + replay_kind → unique violation
        session.add(CounterfactualReplays(**row1))
        session.flush()
        session.add(CounterfactualReplays(**row2))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_replay_kind_check_rejects_invalid_value(self, session: Session) -> None:
        row = encode_counterfactual_replay(_evaluated_equity_record())
        row["replay_kind"] = "INVALID_KIND"
        session.add(CounterfactualReplays(**row))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_replay_status_check_rejects_invalid_value(self, session: Session) -> None:
        row = encode_counterfactual_replay(_evaluated_equity_record())
        row["replay_status"] = "INVALID_STATUS"
        session.add(CounterfactualReplays(**row))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_unevaluable_reason_check_rejects_invalid_value(self, session: Session) -> None:
        row = encode_counterfactual_replay(_unevaluable_record())
        row["unevaluable_reason"] = "NOT_A_REASON"
        session.add(CounterfactualReplays(**row))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_exit_leg_check_rejects_invalid_value(self, session: Session) -> None:
        row = encode_counterfactual_replay(_evaluated_equity_record())
        row["exit_leg"] = "NOT_A_LEG"
        session.add(CounterfactualReplays(**row))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_confidence_check_rejects_invalid_value(self, session: Session) -> None:
        row = encode_counterfactual_replay(_evaluated_equity_record())
        row["confidence"] = "SUPER_HIGH"
        session.add(CounterfactualReplays(**row))
        with pytest.raises(IntegrityError):
            session.flush()

    def _get_ddl(self, engine: Engine) -> str:
        """Fetch the CREATE TABLE DDL for counterfactual_replays from sqlite_master."""
        query = "SELECT sql FROM sqlite_master WHERE type='table' AND name='counterfactual_replays'"
        with engine.connect() as conn:
            return str(conn.execute(text(query)).scalar_one())

    def test_check_vocab_includes_all_five_exit_leg_values(self, engine: Engine) -> None:
        """All five ExitLeg members including STRATEGIST_CLOSE_AT_PROPOSAL must be in DDL."""
        ddl = self._get_ddl(engine)
        for member in ExitLeg:
            assert f"'{member.value}'" in ddl, f"Missing {member.value!r} from exit_leg CHECK"

    def test_check_vocab_includes_all_five_unevaluable_reason_values(self, engine: Engine) -> None:
        """All five UnevaluableReason members must be in DDL from the start."""
        ddl = self._get_ddl(engine)
        for member in UnevaluableReason:
            assert f"'{member.value}'" in ddl, (
                f"Missing {member.value!r} from unevaluable_reason CHECK"
            )


# ---------------------------------------------------------------------------
# Slice 2: Codec round-trips
# ---------------------------------------------------------------------------


class TestCodecRoundTrips:
    def test_evaluated_equity_round_trip(self) -> None:
        record = _evaluated_equity_record()
        encoded = encode_counterfactual_replay(record)
        decoded = decode_counterfactual_replay(encoded)
        assert decoded == record

    def test_evaluated_strategist_close_round_trip(self) -> None:
        """strategist_close_at_proposal exit leg survives encode/decode."""
        record = _evaluated_strategist_close_record()
        encoded = encode_counterfactual_replay(record)
        decoded = decode_counterfactual_replay(encoded)
        assert decoded == record

    def test_unevaluable_round_trip(self) -> None:
        record = _unevaluable_record()
        encoded = encode_counterfactual_replay(record)
        decoded = decode_counterfactual_replay(encoded)
        assert decoded == record

    def test_entered_false_round_trip(self) -> None:
        """entry_window_expired_unfilled shape: entered=False, no exit/pl fields."""
        record = _entered_false_record()
        encoded = encode_counterfactual_replay(record)
        decoded = decode_counterfactual_replay(encoded)
        assert decoded == record

    def test_all_unevaluable_reason_values_encode_to_lowercase(self) -> None:
        """Each UnevaluableReason encodes to its lowercase .value."""
        for reason in UnevaluableReason:
            record = _unevaluable_record(
                replay_id=f"rpl-{reason.value}",
                envelope_id=f"ENV-REC-{reason.value[:4]}",
                reason=reason,
            )
            encoded = encode_counterfactual_replay(record)
            assert encoded["unevaluable_reason"] == reason.value

    def test_all_exit_leg_values_encode_to_lowercase(self) -> None:
        """Each ExitLeg (for entered=True shapes) encodes to its lowercase .value."""
        for leg in (
            ExitLeg.TARGET_HIT,
            ExitLeg.STOP_HIT,
            ExitLeg.TIME_STOP_FIRED,
            ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL,
        ):
            record = CounterfactualReplayRecord(
                replay_id=ReplayId(f"rpl-{leg.value}"),
                pm_decision_envelope_id=EnvelopeId("ENV-REC-99"),
                replay_kind=ReplayKind.REJECTION,
                replay_status=ReplayStatus.EVALUATED,
                unevaluable_reason=None,
                entered=True,
                entry_price=price(Decimal("100.00")),
                entry_timestamp=_TS,
                entry_slippage=money(Decimal("0.01")),
                entry_fees=money(Decimal("0.02")),
                exit_leg=leg,
                exit_price=price(Decimal("105.00")),
                exit_timestamp=_TS2,
                exit_slippage=money(Decimal("0.01")),
                exit_fees=money(Decimal("0.02")),
                realized_pl=Money(Decimal("4.96")),
                confidence=Confidence.HIGH,
                replay_timestamp=_TS3,
                replay_data_window_start=_TS,
                replay_data_window_end=_TS2,
                replay_engine_version="v2.0.0",
            )
            encoded = encode_counterfactual_replay(record)
            assert encoded["exit_leg"] == leg.value

    def test_confidence_encodes_to_lowercase(self) -> None:
        for conf in Confidence:
            record = _evaluated_equity_record()
            varied = dataclasses.replace(
                record,
                confidence=conf,
                replay_id=ReplayId(f"rpl-conf-{conf.value}"),
            )
            encoded = encode_counterfactual_replay(varied)
            assert encoded["confidence"] == conf.value

    def test_replay_kind_encodes_to_lowercase(self) -> None:
        for kind in ReplayKind:
            record = _evaluated_equity_record(kind=kind, replay_id=f"rpl-kind-{kind.value}")
            encoded = encode_counterfactual_replay(record)
            assert encoded["replay_kind"] == kind.value

    def test_replay_status_encodes_to_lowercase(self) -> None:
        record = _evaluated_equity_record()
        encoded = encode_counterfactual_replay(record)
        assert encoded["replay_status"] == "evaluated"

        record_u = _unevaluable_record()
        encoded_u = encode_counterfactual_replay(record_u)
        assert encoded_u["replay_status"] == "unevaluable"

    def test_timestamps_serialized_as_iso_strings(self) -> None:
        record = _evaluated_equity_record()
        encoded = encode_counterfactual_replay(record)
        # Must be strings, not datetime objects
        assert isinstance(encoded["replay_timestamp"], str)
        assert isinstance(encoded["entry_timestamp"], str)
        assert isinstance(encoded["exit_timestamp"], str)

    def test_none_timestamps_stay_none(self) -> None:
        record = _unevaluable_record()
        encoded = encode_counterfactual_replay(record)
        assert encoded["entry_timestamp"] is None
        assert encoded["exit_timestamp"] is None
        assert encoded["replay_data_window_start"] is None
        assert encoded["replay_data_window_end"] is None
