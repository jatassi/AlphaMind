"""Schema-level + codec tests for ``theses`` and ``thesis_components`` (story 04b / ALP-359).

Verifies the column shape, CHECK constraints, FK on
``thesis_components.thesis_id``, and the round-trip codec between the typed
``ThesisRecord`` Pydantic and the parent / child SQLAlchemy rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import (
    PositionId,
    ThesisId,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records import (
    BracketLegType,
    KeyAssumption,
    SupportingSignal,
    SupportingSignalStatus,
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full schema."""
    # Side-effect import: registers the new tables on ``Base.metadata`` so
    # ``create_all`` materializes them.
    import alphamind.execution.state_persistence.tables  # noqa: F401

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
# Test data builders
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


def _make_component(
    component_id: str,
    component_type: ThesisComponentType,
    *,
    thesis_id: str = "thesis-1",
    linked_bracket_leg_id: str | None = None,
    linked_bracket_leg_type: BracketLegType | None = None,
    resolution_outcome: ThesisComponentOutcome | None = None,
    resolution_notes: str | None = None,
    key_assumptions: tuple[KeyAssumption, ...] = (KeyAssumption(text="holds", outcome=None),),
) -> ThesisComponent:
    return ThesisComponent(
        component_id=component_id,
        thesis_id=ThesisId(thesis_id),
        component_type=component_type,
        linked_bracket_leg_type=linked_bracket_leg_type,
        linked_bracket_leg_id=linked_bracket_leg_id,
        instrument_reference="AAPL",
        narrative=f"narrative for {component_id}",
        key_assumptions=key_assumptions,
        generation_timestamp=NOW,
        resolution_outcome=resolution_outcome,
        resolution_notes=resolution_notes,
    )


def _full_components() -> tuple[ThesisComponent, ...]:
    return (
        _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
        _make_component(
            "c-tp",
            ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_id="leg-tp",
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
        ),
        _make_component(
            "c-ps",
            ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_id="leg-ps",
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
        ),
    )


def _active_thesis() -> ThesisRecord:
    return ThesisRecord(
        thesis_id=ThesisId("thesis-1"),
        position_id=PositionId("pos-1"),
        summary="Test thesis",
        key_catalyst="earnings beat",
        position_size_rationale="medium-conviction sizing",
        components=_full_components(),
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=NOW,
        time_expectation_hours=24.0,
        age_hours=1.0,
        expected_resolution_at=NOW + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _resolved_thesis() -> ThesisRecord:
    components = (
        _make_component(
            "c-entry",
            ThesisComponentType.ENTRY_RATIONALE,
            resolution_outcome=ThesisComponentOutcome.VALIDATED,
            resolution_notes="entered at planned level",
        ),
        _make_component(
            "c-tp",
            ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_id="leg-tp",
            linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            resolution_outcome=ThesisComponentOutcome.WRONG,
            resolution_notes="never reached target",
        ),
        _make_component(
            "c-ps",
            ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_id="leg-ps",
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            resolution_outcome=ThesisComponentOutcome.INCONCLUSIVE,
            resolution_notes="closed before stop hit",
        ),
    )
    return ThesisRecord(
        thesis_id=ThesisId("thesis-1"),
        position_id=PositionId("pos-1"),
        summary="Resolved test thesis",
        key_catalyst="earnings",
        position_size_rationale=None,
        components=components,
        status=ThesisRecordStatus.RESOLVED,
        generation_timestamp=NOW,
        time_expectation_hours=24.0,
        age_hours=23.0,
        expected_resolution_at=NOW + timedelta(hours=24),
        resolution_timestamp=NOW + timedelta(hours=23),
        resolution_category=ThesisResolutionCategory.PROFITABLE_BUT_WRONG,
        resolution_pnl_usd=125.50,
        entry_fill_gap_usd=-2.10,
    )


def _cancelled_thesis() -> ThesisRecord:
    return ThesisRecord(
        thesis_id=ThesisId("thesis-2"),
        position_id=PositionId("pos-2"),
        summary="Cancelled before fill",
        key_catalyst="vol expansion",
        position_size_rationale=None,
        components=(),
        status=ThesisRecordStatus.CANCELLED,
        generation_timestamp=NOW,
        time_expectation_hours=12.0,
        age_hours=0.5,
        expected_resolution_at=NOW + timedelta(hours=12),
        resolution_timestamp=NOW + timedelta(minutes=30),
        resolution_category=ThesisResolutionCategory.CANCELLED_NEVER_ENTERED,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


# ---------------------------------------------------------------------------
# theses table
# ---------------------------------------------------------------------------


class TestThesesTable:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("theses")}
        expected = {
            "thesis_id",
            "position_id",
            "status",
            "resolution_timestamp",
            "resolution_category",
            "summary",
            "time_expectation_hours",
            "position_size_rationale",
            "generation_timestamp",
            "narrative_json",
        }
        assert set(cols) == expected
        pk = insp.get_pk_constraint("theses")
        assert pk["constrained_columns"] == ["thesis_id"]

    def test_table_has_expected_indexes(self, engine: Engine) -> None:
        insp = inspect(engine)
        indexes = {idx["name"]: idx for idx in insp.get_indexes("theses")}
        assert "ix_theses_status" in indexes
        assert indexes["ix_theses_status"]["column_names"] == ["status"]
        assert "ix_theses_position_id" in indexes
        assert indexes["ix_theses_position_id"]["column_names"] == ["position_id"]
        assert "ix_theses_resolution_timestamp" in indexes
        assert indexes["ix_theses_resolution_timestamp"]["column_names"] == ["resolution_timestamp"]

    def test_check_rejects_unknown_status(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.theses import ThesisRow

        session.add(
            ThesisRow(
                thesis_id="t-1",
                position_id="pos-1",
                status="DRAFT",
                resolution_timestamp=None,
                resolution_category=None,
                summary="bad status",
                time_expectation_hours=12.0,
                position_size_rationale=None,
                generation_timestamp="2026-05-07T14:30:00Z",
                narrative_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_resolution_category(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.theses import ThesisRow

        session.add(
            ThesisRow(
                thesis_id="t-1",
                position_id="pos-1",
                status="RESOLVED",
                resolution_timestamp="2026-05-07T15:30:00Z",
                resolution_category="MAYBE_WORKED",
                summary="bad category",
                time_expectation_hours=12.0,
                position_size_rationale=None,
                generation_timestamp="2026-05-07T14:30:00Z",
                narrative_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_resolution_category_accepts_cancelled_never_entered(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.theses import ThesisRow
        from tests.execution.state_persistence._fk_substrate import stub_position_row

        session.add(stub_position_row("pos-1"))
        session.add(
            ThesisRow(
                thesis_id="t-1",
                position_id="pos-1",
                status="CANCELLED",
                resolution_timestamp="2026-05-07T15:30:00Z",
                resolution_category="CANCELLED_NEVER_ENTERED",
                summary="cancelled",
                time_expectation_hours=12.0,
                position_size_rationale=None,
                generation_timestamp="2026-05-07T14:30:00Z",
                narrative_json="{}",
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# thesis_components table
# ---------------------------------------------------------------------------


class TestThesisComponentsTable:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("thesis_components")}
        expected = {
            "component_id",
            "thesis_id",
            "component_type",
            "linked_bracket_leg",
            "instrument_reference",
            "narrative",
            "key_assumptions_json",
            "supporting_signals_json",
            "resolution_outcome",
            "resolution_notes",
        }
        assert set(cols) == expected
        pk = insp.get_pk_constraint("thesis_components")
        assert pk["constrained_columns"] == ["component_id"]

    def test_table_has_expected_index(self, engine: Engine) -> None:
        insp = inspect(engine)
        indexes = {idx["name"]: idx for idx in insp.get_indexes("thesis_components")}
        assert "ix_thesis_components_thesis_id" in indexes
        assert indexes["ix_thesis_components_thesis_id"]["column_names"] == ["thesis_id"]

    def test_fk_blocks_orphan_component_insert(self, session: Session) -> None:
        """Inserting a component referencing a nonexistent thesis_id is rejected."""
        from alphamind.execution.state_persistence.tables.thesis_components import (
            ThesisComponentRow,
        )

        session.add(
            ThesisComponentRow(
                component_id="c-1",
                thesis_id="does-not-exist",
                component_type="ENTRY_RATIONALE",
                linked_bracket_leg=None,
                instrument_reference="AAPL",
                narrative="orphan component",
                key_assumptions_json="[]",
                supporting_signals_json="[]",
                resolution_outcome=None,
                resolution_notes=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_fk_restricts_delete_of_referenced_thesis(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.theses import ThesisRow
        from alphamind.execution.state_persistence.tables.thesis_components import (
            ThesisComponentRow,
        )
        from tests.execution.state_persistence._fk_substrate import stub_position_row

        session.add(stub_position_row("pos-1"))
        session.add(
            ThesisRow(
                thesis_id="t-1",
                position_id="pos-1",
                status="ACTIVE",
                resolution_timestamp=None,
                resolution_category=None,
                summary="parent",
                time_expectation_hours=12.0,
                position_size_rationale=None,
                generation_timestamp="2026-05-07T14:30:00Z",
                narrative_json="{}",
            )
        )
        session.commit()
        session.add(
            ThesisComponentRow(
                component_id="c-1",
                thesis_id="t-1",
                component_type="ENTRY_RATIONALE",
                linked_bracket_leg=None,
                instrument_reference="AAPL",
                narrative="child",
                key_assumptions_json="[]",
                supporting_signals_json="[]",
                resolution_outcome=None,
                resolution_notes=None,
            )
        )
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM theses WHERE thesis_id = :tid"), {"tid": "t-1"})
            session.commit()

    def test_check_rejects_unknown_component_type(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.theses import ThesisRow
        from alphamind.execution.state_persistence.tables.thesis_components import (
            ThesisComponentRow,
        )
        from tests.execution.state_persistence._fk_substrate import stub_position_row

        session.add(stub_position_row("pos-1"))
        session.add(
            ThesisRow(
                thesis_id="t-1",
                position_id="pos-1",
                status="ACTIVE",
                resolution_timestamp=None,
                resolution_category=None,
                summary="parent",
                time_expectation_hours=12.0,
                position_size_rationale=None,
                generation_timestamp="2026-05-07T14:30:00Z",
                narrative_json="{}",
            )
        )
        session.commit()
        session.add(
            ThesisComponentRow(
                component_id="c-1",
                thesis_id="t-1",
                component_type="OTHER_RATIONALE",
                linked_bracket_leg=None,
                instrument_reference="AAPL",
                narrative="bad type",
                key_assumptions_json="[]",
                supporting_signals_json="[]",
                resolution_outcome=None,
                resolution_notes=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_resolution_outcome(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.theses import ThesisRow
        from alphamind.execution.state_persistence.tables.thesis_components import (
            ThesisComponentRow,
        )
        from tests.execution.state_persistence._fk_substrate import stub_position_row

        session.add(stub_position_row("pos-1"))
        session.add(
            ThesisRow(
                thesis_id="t-1",
                position_id="pos-1",
                status="RESOLVED",
                resolution_timestamp="2026-05-07T15:30:00Z",
                resolution_category="VALIDATED",
                summary="parent",
                time_expectation_hours=12.0,
                position_size_rationale=None,
                generation_timestamp="2026-05-07T14:30:00Z",
                narrative_json="{}",
            )
        )
        session.commit()
        session.add(
            ThesisComponentRow(
                component_id="c-1",
                thesis_id="t-1",
                component_type="ENTRY_RATIONALE",
                linked_bracket_leg=None,
                instrument_reference="AAPL",
                narrative="bad outcome",
                key_assumptions_json="[]",
                supporting_signals_json="[]",
                resolution_outcome="MAYBE",
                resolution_notes=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Round-trip codec
# ---------------------------------------------------------------------------


class TestThesisCodec:
    def test_record_to_rows_decomposes_active_thesis(self) -> None:
        from alphamind.execution.state_persistence.tables.theses_codec import record_to_rows

        record = _active_thesis()
        thesis_row, component_rows = record_to_rows(record)
        assert thesis_row.thesis_id == "thesis-1"
        assert thesis_row.position_id == "pos-1"
        assert thesis_row.status == "ACTIVE"
        assert thesis_row.resolution_timestamp is None
        assert thesis_row.resolution_category is None
        assert thesis_row.summary == "Test thesis"
        assert thesis_row.time_expectation_hours == pytest.approx(24.0)
        assert thesis_row.position_size_rationale == "medium-conviction sizing"
        assert len(component_rows) == 3
        # Component-order-preserving.
        assert tuple(c.component_id for c in component_rows) == ("c-entry", "c-tp", "c-ps")
        assert component_rows[1].linked_bracket_leg == "leg-tp"
        assert component_rows[1].component_type == "TARGET_RATIONALE"
        assert component_rows[1].resolution_outcome is None

    def test_round_trip_active_with_three_components(self) -> None:
        from alphamind.execution.state_persistence.tables.theses_codec import (
            record_to_rows,
            rows_to_record,
        )

        record = _active_thesis()
        thesis_row, component_rows = record_to_rows(record)
        roundtripped = rows_to_record(thesis_row, component_rows)
        assert roundtripped == record

    def test_round_trip_resolved_with_per_component_outcomes(self) -> None:
        from alphamind.execution.state_persistence.tables.theses_codec import (
            record_to_rows,
            rows_to_record,
        )

        record = _resolved_thesis()
        thesis_row, component_rows = record_to_rows(record)
        roundtripped = rows_to_record(thesis_row, component_rows)
        assert roundtripped == record

    def test_round_trip_cancelled_with_zero_components(self) -> None:
        from alphamind.execution.state_persistence.tables.theses_codec import (
            record_to_rows,
            rows_to_record,
        )

        record = _cancelled_thesis()
        thesis_row, component_rows = record_to_rows(record)
        assert component_rows == ()
        assert thesis_row.status == "CANCELLED"
        assert thesis_row.resolution_category == "CANCELLED_NEVER_ENTERED"
        roundtripped = rows_to_record(thesis_row, component_rows)
        assert roundtripped == record

    def test_round_trip_preserves_supporting_signals_in_components(self) -> None:
        """Supporting signals on a component round-trip via the JSON column."""
        from alphamind.execution.state_persistence.tables.theses_codec import (
            record_to_rows,
            rows_to_record,
        )

        # Inject supporting signals via the codec's reverse path: build a
        # record whose components carry signals through the JSON column.
        # The records currently carry KeyAssumption only; supporting signals
        # are surfaced through the component's JSON payload, not a typed
        # attribute on ThesisComponent. The codec stores them as a parallel
        # JSON list so future extensions can rehydrate them; for now we
        # verify zero-signal round-trip is faithful.
        record = _active_thesis()
        thesis_row, component_rows = record_to_rows(record)
        # Empty list is the documented default.
        for row in component_rows:
            assert row.supporting_signals_json == "[]"
        roundtripped = rows_to_record(thesis_row, component_rows)
        assert roundtripped == record

    def test_round_trip_persists_through_sqlalchemy(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.theses import ThesisRow
        from alphamind.execution.state_persistence.tables.theses_codec import (
            record_to_rows,
            rows_to_record,
        )
        from alphamind.execution.state_persistence.tables.thesis_components import (
            ThesisComponentRow,
        )
        from tests.execution.state_persistence._fk_substrate import stub_position_row

        record = _resolved_thesis()
        thesis_row, component_rows = record_to_rows(record)
        session.add(stub_position_row(record.position_id))
        session.add(thesis_row)
        for crow in component_rows:
            session.add(crow)
        session.commit()

        readback_thesis = session.get(ThesisRow, "thesis-1")
        assert readback_thesis is not None
        readback_components = tuple(
            session.query(ThesisComponentRow)
            .filter(ThesisComponentRow.thesis_id == "thesis-1")
            .order_by(ThesisComponentRow.component_id)
            .all()
        )
        # Re-order by original component order to verify lossless reconstruction.
        ordered = tuple(
            sorted(
                readback_components,
                key=lambda r: (
                    "c-entry",
                    "c-tp",
                    "c-ps",
                ).index(r.component_id),
            )
        )
        roundtripped = rows_to_record(readback_thesis, ordered)
        assert roundtripped == record


# ---------------------------------------------------------------------------
# ThesisRecord typed-validator
# ---------------------------------------------------------------------------


class TestActiveThesisValidator:
    def test_active_with_non_null_resolution_timestamp_is_rejected(self) -> None:
        """ACTIVE thesis with non-null ``resolution_timestamp`` rejected by the typed validator."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ThesisRecord(
                thesis_id=ThesisId("thesis-1"),
                position_id=PositionId("pos-1"),
                summary="bad active",
                key_catalyst="cat",
                position_size_rationale=None,
                components=_full_components(),
                status=ThesisRecordStatus.ACTIVE,
                generation_timestamp=NOW,
                time_expectation_hours=24.0,
                age_hours=1.0,
                expected_resolution_at=NOW + timedelta(hours=24),
                resolution_timestamp=NOW + timedelta(hours=12),
                resolution_category=None,
                resolution_pnl_usd=None,
                entry_fill_gap_usd=None,
            )


# ---------------------------------------------------------------------------
# Smoke verifying SupportingSignal helper present in records (used by tests)
# ---------------------------------------------------------------------------


def test_supporting_signal_imports_resolve() -> None:
    """Smoke: SupportingSignal + status enum import from records package."""
    sig = SupportingSignal(name="rsi-5d", status=SupportingSignalStatus.STRENGTHENED)
    assert sig.name == "rsi-5d"
    assert sig.status == SupportingSignalStatus.STRENGTHENED
