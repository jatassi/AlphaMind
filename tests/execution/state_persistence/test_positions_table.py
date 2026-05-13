"""Tests for the positions SQL table + round-trip codec (story 04a / ALP-358).

Covers:
- ``PositionRow`` SQLAlchemy model: column shape, indexes, CHECK constraints.
- ``record_to_row`` / ``row_to_record`` faithful round-trip across the three
  ``PositionDetailsPayload`` variants (equity, options, strategy).
- Pydantic-side rejection of invalid status / discriminator combinations.
- Alembic migration: idempotent up + down, column / index / CHECK shape.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import (
    BracketId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row,
    row_to_record,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LiveExecutionEstimate,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full schema."""
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
# Builders — one per variant, each non-trivial
# ---------------------------------------------------------------------------


_FILL_TS = datetime(2026, 5, 7, 14, 30, tzinfo=UTC)


def _equity_position(
    *,
    position_id: str = "pos-eq-1",
    direction: Direction = Direction.LONG,
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    is_short = direction == Direction.SHORT
    fills: tuple[PositionFill, ...] = (
        PositionFill(
            fill_timestamp=_FILL_TS,
            fill_price=150.25,
            fill_quantity=60.0,
            slippage=0.05,
            fees=1.25,
        ),
        PositionFill(
            fill_timestamp=datetime(2026, 5, 7, 14, 31, tzinfo=UTC),
            fill_price=150.40,
            fill_quantity=40.0,
            slippage=0.10,
            fees=1.00,
            live_execution_estimate=LiveExecutionEstimate(
                estimated_spread_usd=0.5,
                estimated_impact_usd=0.25,
                estimated_regulatory_fees_usd=0.05,
                live_adjusted_fill_price=150.45,
            ),
        ),
    )
    if status == PositionStatus.PENDING:
        fills = ()
    realized = 12.50 if status == PositionStatus.CLOSED else None
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("thesis-1"),
        bracket_id=BracketId("bracket-1"),
        status=status,
        direction=direction,
        entry_timestamp=None if status == PositionStatus.PENDING else _FILL_TS,
        details=EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=100.0,
            average_cost_basis_per_share=150.31,
            borrow_rate_pct=2.5 if is_short else None,
            locate_status=LocateStatus.LOCATED if is_short else None,
            margin_held_usd=3000.0 if is_short else None,
        ),
        execution_history=fills,
        realized_pnl_to_date_usd=realized,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _options_position(*, position_id: str = "pos-opt-1") -> PositionRecord:
    greeks = OptionGreeks(
        delta=0.55,
        gamma=0.03,
        theta=-0.02,
        vega=0.18,
        as_of_timestamp=datetime(2026, 5, 7, 15, 0, tzinfo=UTC),
        iv_used=0.32,
        refresh_failed=False,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("thesis-opt-1"),
        bracket_id=BracketId("bracket-opt-1"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_FILL_TS,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("MSFT"),
            strike_price=420.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=5.0,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            premium_paid_per_contract=8.75,
            greeks=greeks,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_FILL_TS,
                fill_price=8.75,
                fill_quantity=5.0,
                slippage=0.02,
                fees=0.65,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(*, position_id: str = "pos-strat-1") -> PositionRecord:
    leg_long = StrategyLeg(
        leg_id="leg-long-call",
        direction=Direction.LONG,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("SPY"),
            strike_price=520.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=10.0,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            premium_paid_per_contract=4.20,
            greeks=OptionGreeks(delta=0.45, gamma=0.04, theta=-0.03, vega=0.20),
        ),
    )
    leg_short = StrategyLeg(
        leg_id="leg-short-call",
        direction=Direction.SHORT,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("SPY"),
            strike_price=530.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=10.0,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            premium_paid_per_contract=2.10,
            greeks=OptionGreeks(delta=0.30, gamma=0.03, theta=-0.025, vega=0.18),
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("thesis-strat-1"),
        bracket_id=BracketId("bracket-strat-1"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_FILL_TS,
        details=StrategyPositionDetails(
            strategy_type_label="bull_call_spread",
            legs=(leg_long, leg_short),
            net_premium_usd=2100.0,
            max_profit_usd=7900.0,
            max_loss_usd=2100.0,
            breakeven_levels=(522.10,),
            strategy_greeks=OptionGreeks(delta=0.15, gamma=0.01, theta=-0.005, vega=0.02),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_FILL_TS,
                fill_price=2.10,
                fill_quantity=10.0,
                slippage=0.03,
                fees=1.30,
            ),
            PositionFill(
                fill_timestamp=datetime(2026, 5, 7, 14, 31, tzinfo=UTC),
                fill_price=4.20,
                fill_quantity=10.0,
                slippage=0.04,
                fees=1.30,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


# ---------------------------------------------------------------------------
# Schema-shape tests
# ---------------------------------------------------------------------------


class TestPositionsTableSchema:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("positions")}
        assert cols == {
            "position_id",
            "thesis_id",
            "bracket_id",
            "status",
            "direction",
            "entry_timestamp",
            "instrument_type",
            "details_json",
            "execution_history_json",
            "realized_pnl_to_date_usd",
            "corporate_action_adjustment_needed",
            "parent_position_id",
            "origin",
        }
        pk = insp.get_pk_constraint("positions")
        assert pk["constrained_columns"] == ["position_id"]

    def test_table_has_expected_indexes(self, engine: Engine) -> None:
        insp = inspect(engine)
        indexes = {idx["name"]: idx for idx in insp.get_indexes("positions")}
        assert "ix_positions_status" in indexes
        assert indexes["ix_positions_status"]["column_names"] == ["status"]
        assert "ix_positions_thesis_id" in indexes
        assert indexes["ix_positions_thesis_id"]["column_names"] == ["thesis_id"]

    def test_check_rejects_unknown_status(self, session: Session) -> None:
        session.add(
            PositionRow(
                position_id="pos-1",
                thesis_id=None,
                bracket_id=None,
                status="UNKNOWN",
                direction=Direction.LONG.value,
                entry_timestamp=None,
                instrument_type=InstrumentType.EQUITY.value,
                details_json="{}",
                execution_history_json="[]",
                realized_pnl_to_date_usd=None,
                corporate_action_adjustment_needed=0,
                parent_position_id=None,
                origin=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_direction(self, session: Session) -> None:
        session.add(
            PositionRow(
                position_id="pos-1",
                thesis_id=None,
                bracket_id=None,
                status=PositionStatus.PENDING.value,
                direction="SIDEWAYS",
                entry_timestamp=None,
                instrument_type=InstrumentType.EQUITY.value,
                details_json="{}",
                execution_history_json="[]",
                realized_pnl_to_date_usd=None,
                corporate_action_adjustment_needed=0,
                parent_position_id=None,
                origin=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_instrument_type(self, session: Session) -> None:
        session.add(
            PositionRow(
                position_id="pos-1",
                thesis_id=None,
                bracket_id=None,
                status=PositionStatus.PENDING.value,
                direction=Direction.LONG.value,
                entry_timestamp=None,
                instrument_type="FUTURES",
                details_json="{}",
                execution_history_json="[]",
                realized_pnl_to_date_usd=None,
                corporate_action_adjustment_needed=0,
                parent_position_id=None,
                origin=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Round-trip codec — equity / options / strategy variants
# ---------------------------------------------------------------------------


class TestPositionRoundTrip:
    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(_equity_position(direction=Direction.LONG), id="equity-long-open"),
            pytest.param(_equity_position(direction=Direction.SHORT), id="equity-short-open"),
            pytest.param(
                _equity_position(status=PositionStatus.PENDING),
                id="equity-pending",
            ),
            pytest.param(
                _equity_position(status=PositionStatus.CLOSED),
                id="equity-closed",
            ),
            pytest.param(_options_position(), id="options-call"),
            pytest.param(_strategy_position(), id="strategy-bull-call-spread"),
        ],
    )
    def test_record_round_trip(self, session: Session, record: PositionRecord) -> None:
        from tests.execution.state_persistence._fk_substrate import (
            stub_bracket_row,
            stub_order_row,
            stub_thesis_row,
        )

        row = record_to_row(record)
        # Seed FK parents in the same transaction (deferred FKs check at COMMIT).
        if row.thesis_id is not None:
            session.add(stub_thesis_row(row.thesis_id, row.position_id))
        if row.bracket_id is not None:
            entry_order_id = f"stub-entry-{row.bracket_id}"
            session.add(stub_order_row(entry_order_id, row.bracket_id))
            session.add(stub_bracket_row(row.bracket_id, row.position_id, entry_order_id))
        session.add(row)
        session.commit()

        fetched = session.get(PositionRow, record.position_id)
        assert fetched is not None
        rehydrated = row_to_record(fetched)
        assert rehydrated == record

    def test_pure_record_to_row_round_trip(self) -> None:
        """Round-trip without going through the DB — codec invariant alone."""
        for record in (
            _equity_position(direction=Direction.LONG),
            _equity_position(direction=Direction.SHORT),
            _options_position(),
            _strategy_position(),
        ):
            assert row_to_record(record_to_row(record)) == record


# ---------------------------------------------------------------------------
# Invariant violations rejected at record_to_row / row_to_record time
# ---------------------------------------------------------------------------


class TestInvariantRejection:
    def test_pending_with_non_empty_execution_history_is_rejected(self) -> None:
        # The Pydantic ``PositionRecord`` validator rejects this combination
        # at construction time, so ``record_to_row`` never sees an invalid
        # record — the SQL CHECK does not need to encode this rule.
        with pytest.raises(ValueError, match="execution_history"):
            PositionRecord(
                position_id=PositionId("pos-bad-pending"),
                thesis_id=None,
                bracket_id=None,
                status=PositionStatus.PENDING,
                direction=Direction.LONG,
                entry_timestamp=None,
                details=EquityPositionDetails(
                    ticker=Symbol("AAPL"),
                    share_count=100.0,
                    average_cost_basis_per_share=150.0,
                ),
                execution_history=(
                    PositionFill(
                        fill_timestamp=_FILL_TS,
                        fill_price=150.0,
                        fill_quantity=100.0,
                        slippage=0.0,
                        fees=0.0,
                    ),
                ),
                realized_pnl_to_date_usd=None,
                corporate_action_adjustment_needed=False,
                parent_position_id=None,
                origin=None,
            )

    def test_closed_with_null_realized_pnl_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="realized_pnl_to_date_usd"):
            PositionRecord(
                position_id=PositionId("pos-bad-closed"),
                thesis_id=None,
                bracket_id=None,
                status=PositionStatus.CLOSED,
                direction=Direction.LONG,
                entry_timestamp=_FILL_TS,
                details=EquityPositionDetails(
                    ticker=Symbol("AAPL"),
                    share_count=100.0,
                    average_cost_basis_per_share=150.0,
                ),
                execution_history=(
                    PositionFill(
                        fill_timestamp=_FILL_TS,
                        fill_price=150.0,
                        fill_quantity=100.0,
                        slippage=0.0,
                        fees=0.0,
                    ),
                ),
                realized_pnl_to_date_usd=None,
                corporate_action_adjustment_needed=False,
                parent_position_id=None,
                origin=None,
            )

    def test_mismatched_instrument_type_and_details_discriminator_is_rejected(
        self, session: Session
    ) -> None:
        """A row whose ``instrument_type`` column disagrees with
        ``details_json``'s discriminator is rejected by ``row_to_record``.

        The mismatch can only arise from a hand-crafted INSERT that bypasses
        the codec; ``record_to_row`` always emits matching values."""
        from tests.execution.state_persistence._fk_substrate import (
            stub_bracket_row,
            stub_order_row,
            stub_thesis_row,
        )

        record = _equity_position()
        row = record_to_row(record)
        row.instrument_type = InstrumentType.OPTIONS.value
        if row.thesis_id is not None:
            session.add(stub_thesis_row(row.thesis_id, row.position_id))
        if row.bracket_id is not None:
            entry_order_id = f"stub-entry-{row.bracket_id}"
            session.add(stub_order_row(entry_order_id, row.bracket_id))
            session.add(stub_bracket_row(row.bracket_id, row.position_id, entry_order_id))
        session.add(row)
        session.commit()

        fetched = session.get(PositionRow, record.position_id)
        assert fetched is not None
        with pytest.raises(ValueError, match="instrument_type"):
            row_to_record(fetched)


# ---------------------------------------------------------------------------
# Alembic migration — idempotency + shape
# ---------------------------------------------------------------------------


_REVISION = "c8e3f4a2b1d6"
_PRIOR_REVISION = "b5d2e3f4c6a7"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestPositionsMigration:
    def test_upgrade_creates_positions_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), _REVISION)

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "positions" in tables

            cols = {c["name"] for c in insp.get_columns("positions")}
            assert cols == {
                "position_id",
                "thesis_id",
                "bracket_id",
                "status",
                "direction",
                "entry_timestamp",
                "instrument_type",
                "details_json",
                "execution_history_json",
                "realized_pnl_to_date_usd",
                "corporate_action_adjustment_needed",
                "parent_position_id",
                "origin",
            }

            indexes = {idx["name"] for idx in insp.get_indexes("positions")}
            assert "ix_positions_status" in indexes
            assert "ix_positions_thesis_id" in indexes
        finally:
            eng.dispose()

    def test_downgrade_drops_positions_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PRIOR_REVISION)

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "positions" not in tables
        finally:
            eng.dispose()

    def test_upgrade_then_downgrade_then_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PRIOR_REVISION)
        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "positions" in set(insp.get_table_names())
        finally:
            eng.dispose()

    def test_migration_check_constraints_reject_invalid_values(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), _REVISION)

        eng = make_engine(str(db_path))
        # Each VALUES row violates exactly one CHECK (status / direction /
        # instrument_type, in order) — the migration's CHECK constraints
        # must reject all three.
        insert_template = (
            "INSERT INTO positions (position_id, status, direction, "
            "instrument_type, details_json, execution_history_json, "
            "corporate_action_adjustment_needed) VALUES "
            "('p1', '{status}', '{direction}', '{instrument_type}', "
            "'{{}}', '[]', 0)"
        )
        bad_inserts = (
            insert_template.format(status="BOGUS", direction="LONG", instrument_type="EQUITY"),
            insert_template.format(status="PENDING", direction="BOGUS", instrument_type="EQUITY"),
            insert_template.format(status="PENDING", direction="LONG", instrument_type="BOGUS"),
        )
        try:
            for sql in bad_inserts:
                with eng.begin() as conn, pytest.raises(IntegrityError):
                    conn.execute(text(sql))
        finally:
            eng.dispose()
