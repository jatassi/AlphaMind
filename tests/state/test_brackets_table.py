"""Tests for the brackets + bracket_legs SQL tables and round-trip codec
(story 04d / ALP-361).

Covers:
- ``BracketRow`` SQLAlchemy model: column shape, indexes, CHECK constraint
  on ``status``.
- ``BracketLegRow`` SQLAlchemy model: column shape, indexes (incl. UNIQUE
  on ``(bracket_id, leg_index)``), CHECK constraints on ``leg_type`` /
  ``trigger_kind`` / ``enforcement`` / ``leg_status``, FK to ``brackets``.
- Round-trip codec: ``record_to_rows`` / ``rows_to_record`` faithful for
  every leg type, every trigger kind, with and without ``PLAnchorSpec``,
  with and without modification history.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import (
    BracketId,
    CommandId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegModification,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EventTrigger,
    PLAnchorSpec,
    PriceTrigger,
    TimeTrigger,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    record_to_rows,
    rows_to_record,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with the full schema + state-persistence tables."""
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
# Builders
# ---------------------------------------------------------------------------


_T0 = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
_T1 = datetime(2026, 5, 7, 16, 30, 0, tzinfo=UTC)


def _take_profit_leg(
    *,
    leg_id: str = "brk1::0",
    order_id: str | None = "ord-tp",
    pl_anchor: PLAnchorSpec | None = None,
) -> BracketLeg:
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId(order_id) if order_id is not None else None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"),
            threshold_usd=200.0,
            direction="GTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
        pl_anchor=pl_anchor,
    )


def _price_stop_leg(
    *,
    leg_id: str = "brk1::1",
    order_id: str | None = "ord-ps",
    pl_anchor: PLAnchorSpec | None = None,
) -> BracketLeg:
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(order_id) if order_id is not None else None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"),
            threshold_usd=140.0,
            direction="LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
        pl_anchor=pl_anchor,
    )


def _time_expiration_leg(
    *,
    leg_id: str = "brk1::2",
    order_id: str | None = "ord-te",
    deadline: datetime = _T1,
) -> BracketLeg:
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.TIME_EXPIRATION,
        order_id=OrderId(order_id) if order_id is not None else None,
        trigger=TimeTrigger(deadline=deadline),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )


def _event_invalidation_leg(
    *,
    leg_id: str = "brk1::3",
) -> BracketLeg:
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.EVENT_INVALIDATION,
        order_id=None,
        trigger=EventTrigger(
            description="Earnings beat fails to lift price",
            condition_evaluator_id="evaluator-1",
        ),
        enforcement=BracketLegEnforcement.ADVISORY,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )


def _three_leg_bracket(
    *,
    bracket_id: str = "brk1",
    position_id: str = "pos1",
    modification_history: tuple[BracketLegModification, ...] = (),
    entry_window_deadline: datetime | None = None,
    corporate_action_cancellation_reason: str | None = None,
) -> BracketRecord:
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("entry-1"),
        protective_legs=(
            _take_profit_leg(leg_id=f"{bracket_id}::0"),
            _price_stop_leg(leg_id=f"{bracket_id}::1"),
            _time_expiration_leg(leg_id=f"{bracket_id}::2"),
        ),
        modification_history=modification_history,
        corporate_action_cancellation_reason=corporate_action_cancellation_reason,
        entry_window_deadline=entry_window_deadline,
    )


def _persist(
    session: Session, record: BracketRecord
) -> tuple[BracketRow, tuple[BracketLegRow, ...]]:
    """Insert a bracket + its legs in one deferred-FK transaction.

    All deferrable FKs (brackets.position_id, brackets.entry_order_id,
    orders.bracket_id, bracket_legs.order_id) are checked at COMMIT, so all
    rows — stub parents and the real bracket rows — must land in a single
    transaction.  The legs are flushed after the bracket parent so the
    non-deferred brackets.bracket_id FK on bracket_legs is satisfied
    immediately at flush time.
    """
    from tests.state._fk_substrate import (
        stub_order_row,
        stub_position_row,
    )

    bracket_row, leg_rows = record_to_rows(record)
    seeded_order_ids: set[str] = set()
    session.add(stub_position_row(record.position_id))
    session.add(stub_order_row(record.entry_order_id, record.bracket_id))
    seeded_order_ids.add(record.entry_order_id)
    session.add(bracket_row)
    session.flush()
    for leg in record.protective_legs:
        if leg.order_id is not None and leg.order_id not in seeded_order_ids:
            session.add(stub_order_row(leg.order_id, record.bracket_id))
            seeded_order_ids.add(leg.order_id)
    session.add_all(leg_rows)
    session.commit()
    return bracket_row, leg_rows


def _read_legs(session: Session, bracket_id: str) -> tuple[BracketLegRow, ...]:
    """Fetch legs for a bracket ordered by leg_index."""
    return tuple(
        session.query(BracketLegRow)
        .filter(BracketLegRow.bracket_id == bracket_id)
        .order_by(BracketLegRow.leg_index)
        .all()
    )


# ---------------------------------------------------------------------------
# brackets table — schema shape
# ---------------------------------------------------------------------------


class TestBracketsTable:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("brackets")}
        assert cols == {
            "bracket_id",
            "position_id",
            "status",
            "entry_order_id",
            "entry_window_deadline",
            "corporate_action_cancellation_reason",
            "modification_history_json",
        }
        pk = insp.get_pk_constraint("brackets")
        assert pk["constrained_columns"] == ["bracket_id"]

    def test_table_has_expected_indexes(self, engine: Engine) -> None:
        insp = inspect(engine)
        indexes = {idx["name"]: idx for idx in insp.get_indexes("brackets")}
        assert "ix_brackets_position_id" in indexes
        assert indexes["ix_brackets_position_id"]["column_names"] == ["position_id"]
        # 1:1 with positions — must be UNIQUE.
        assert bool(indexes["ix_brackets_position_id"]["unique"]) is True
        assert "ix_brackets_status" in indexes
        assert indexes["ix_brackets_status"]["column_names"] == ["status"]

    def test_check_rejects_unknown_status(self, session: Session) -> None:
        session.add(
            BracketRow(
                bracket_id="brk1",
                position_id="pos1",
                status="BOGUS_STATUS",
                entry_order_id="entry-1",
                entry_window_deadline=None,
                corporate_action_cancellation_reason=None,
                modification_history_json="[]",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# bracket_legs table — schema shape
# ---------------------------------------------------------------------------


class TestBracketLegsTable:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("bracket_legs")}
        assert cols == {
            "bracket_leg_id",
            "bracket_id",
            "leg_index",
            "leg_type",
            "order_id",
            "trigger_kind",
            "trigger_payload_json",
            "pl_anchor_json",
            "enforcement",
            "leg_status",
        }
        pk = insp.get_pk_constraint("bracket_legs")
        assert pk["constrained_columns"] == ["bracket_leg_id"]

    def test_table_has_expected_indexes(self, engine: Engine) -> None:
        insp = inspect(engine)
        indexes = {idx["name"]: idx for idx in insp.get_indexes("bracket_legs")}
        assert "ix_bracket_legs_bracket_id" in indexes
        assert indexes["ix_bracket_legs_bracket_id"]["column_names"] == ["bracket_id"]
        # UNIQUE constraint on (bracket_id, leg_index).
        assert "uq_bracket_legs_bracket_id_leg_index" in indexes
        uq = indexes["uq_bracket_legs_bracket_id_leg_index"]
        assert uq["column_names"] == ["bracket_id", "leg_index"]
        assert bool(uq["unique"]) is True

    def test_fk_to_brackets_blocks_orphan_insert(self, session: Session) -> None:
        # No brackets row exists; the FK must reject this insert.
        session.add(
            BracketLegRow(
                bracket_leg_id="missing::0",
                bracket_id="missing-bracket",
                leg_index=0,
                leg_type=BracketLegType.TAKE_PROFIT.value,
                order_id="ord-tp",
                trigger_kind="PRICE",
                trigger_payload_json="{}",
                pl_anchor_json=None,
                enforcement=BracketLegEnforcement.MECHANICAL.value,
                leg_status=BracketLegStatus.PENDING_ACTIVATION.value,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_unique_constraint_rejects_duplicate_leg_index(self, session: Session) -> None:
        from tests.state._fk_substrate import stub_order_row

        _persist(session, _three_leg_bracket())

        session.add(stub_order_row("ord-other", "brk1"))
        # Insert a leg duplicating the existing leg_index=0 within the bracket.
        session.add(
            BracketLegRow(
                bracket_leg_id="duplicate-id",
                bracket_id="brk1",
                leg_index=0,
                leg_type=BracketLegType.TAKE_PROFIT.value,
                order_id="ord-other",
                trigger_kind="PRICE",
                trigger_payload_json="{}",
                pl_anchor_json=None,
                enforcement=BracketLegEnforcement.MECHANICAL.value,
                leg_status=BracketLegStatus.PENDING_ACTIVATION.value,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("leg_type", "UNKNOWN_LEG_TYPE"),
            ("trigger_kind", "OTHER"),
            ("enforcement", "OTHER_ENFORCEMENT"),
            ("leg_status", "OTHER_STATUS"),
        ],
    )
    def test_check_rejects_unknown_vocabulary_value(
        self, session: Session, field: str, bad_value: str
    ) -> None:
        from tests.state._fk_substrate import stub_order_row

        # Seed the parent bracket so the FK is satisfied; the CHECK fires
        # on the child insert below.
        _persist(session, _three_leg_bracket())

        session.add(stub_order_row("ord-x", "brk1"))
        kwargs: dict[str, object] = {
            "bracket_leg_id": "brk1::99",
            "bracket_id": "brk1",
            "leg_index": 99,
            "leg_type": BracketLegType.TAKE_PROFIT.value,
            "order_id": "ord-x",
            "trigger_kind": "PRICE",
            "trigger_payload_json": "{}",
            "pl_anchor_json": None,
            "enforcement": BracketLegEnforcement.MECHANICAL.value,
            "leg_status": BracketLegStatus.PENDING_ACTIVATION.value,
        }
        kwargs[field] = bad_value
        session.add(BracketLegRow(**kwargs))
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Round-trip codec
# ---------------------------------------------------------------------------


def _round_trip(session: Session, original: BracketRecord) -> BracketRecord:
    """Persist a record then rehydrate via the codec."""
    _persist(session, original)
    bracket_row = session.get(BracketRow, original.bracket_id)
    assert bracket_row is not None
    leg_rows = _read_legs(session, original.bracket_id)
    return rows_to_record(bracket_row, leg_rows)


class TestBracketCodecRoundTrip:
    def test_three_leg_mechanical_bracket_round_trips(self, session: Session) -> None:
        original = _three_leg_bracket()
        assert _round_trip(session, original) == original

    def test_event_invalidation_advisory_leg_round_trips(self, session: Session) -> None:
        # An event-invalidation advisory leg has no order_id; the bracket still
        # needs a mechanical backstop, so we pair it with a price-stop leg.
        original = BracketRecord(
            bracket_id=BracketId("brk-event"),
            position_id=PositionId("pos-event"),
            status=BracketStatus.PENDING_ENTRY,
            entry_order_id=OrderId("entry-event"),
            protective_legs=(
                _price_stop_leg(leg_id="brk-event::0"),
                _event_invalidation_leg(leg_id="brk-event::1"),
            ),
            modification_history=(),
            corporate_action_cancellation_reason=None,
            entry_window_deadline=None,
        )
        assert _round_trip(session, original) == original
        # Event-invalidation leg has order_id None, pl_anchor_json None,
        # trigger_kind EVENT.
        event_row = _read_legs(session, "brk-event")[1]
        assert event_row.order_id is None
        assert event_row.pl_anchor_json is None
        assert event_row.trigger_kind == "EVENT"

    def test_modification_history_round_trips(self, session: Session) -> None:
        history = (
            BracketLegModification(
                timestamp=_T0,
                pm_command_id=CommandId("cmd-1"),
                source="PM",
                field_changed="threshold_usd",
                old_value="140.0",
                new_value="135.0",
                rationale="tighten stop after support broke",
            ),
            BracketLegModification(
                timestamp=_T1,
                pm_command_id=None,
                source="FILL_ANCHOR_RECALCULATION",
                field_changed="pl_anchor.actual_entry_price",
                old_value="None",
                new_value="150.25",
                rationale="recalc on entry fill",
            ),
        )
        original = _three_leg_bracket(modification_history=history)
        rehydrated = _round_trip(session, original)
        assert rehydrated == original
        assert rehydrated.modification_history == history

    def test_pl_anchor_present_on_take_profit_round_trips(self, session: Session) -> None:
        anchor = PLAnchorSpec(
            spec_type="target",
            pct=0.8,
            planned_entry_price=150.0,
            actual_entry_price=None,
            recalculated_at_fill=False,
        )
        leg_with_anchor = _take_profit_leg(leg_id="brk-anchor::0", pl_anchor=anchor)
        # Pair with a price-stop for hard-backstop satisfaction.
        original = BracketRecord(
            bracket_id=BracketId("brk-anchor"),
            position_id=PositionId("pos-anchor"),
            status=BracketStatus.PENDING_ENTRY,
            entry_order_id=OrderId("entry-anchor"),
            protective_legs=(leg_with_anchor, _price_stop_leg(leg_id="brk-anchor::1")),
            modification_history=(),
            corporate_action_cancellation_reason=None,
            entry_window_deadline=None,
        )
        assert _round_trip(session, original) == original
        # pl_anchor_json present on the take-profit leg, NULL on the stop leg.
        legs = _read_legs(session, "brk-anchor")
        assert legs[0].pl_anchor_json is not None
        assert legs[1].pl_anchor_json is None

    def test_leg_index_preserves_ordering(self) -> None:
        original = _three_leg_bracket()
        _, leg_rows = record_to_rows(original)
        # leg_index values are 0, 1, 2 in the order of protective_legs.
        assert tuple(r.leg_index for r in leg_rows) == (0, 1, 2)
        # leg_type sequence matches the record's protective_legs order.
        assert tuple(r.leg_type for r in leg_rows) == (
            BracketLegType.TAKE_PROFIT.value,
            BracketLegType.PRICE_STOP.value,
            BracketLegType.TIME_EXPIRATION.value,
        )

    def test_entry_window_deadline_round_trips(self, session: Session) -> None:
        original = _three_leg_bracket(
            entry_window_deadline=_T1,
            corporate_action_cancellation_reason="symbol delisted",
        )
        rehydrated = _round_trip(session, original)
        assert rehydrated == original
        assert rehydrated.entry_window_deadline == _T1
        assert rehydrated.corporate_action_cancellation_reason == "symbol delisted"
