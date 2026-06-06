"""Tests for ``load_position_state_at`` (ALP-563, story 07 §1).

Covers the as-of position-state reconstruction the strategist replay path reads
before simulating a CLOSE / REDUCE / ADJUST-BRACKET / ADD:

* happy path — equity + option positions, no pre-``as_of`` adds (snapshot equals
  the position's current ``details``);
* as-of reconstruction — a pre-``as_of`` ADD fill folds into a quantity-weighted
  ``average_cost_basis`` and a signed ``net_quantity_as_of``;
* a fill *after* ``as_of`` is excluded from the fold;
* miss (no such position) and multi-leg strategy both raise
  ``PositionStateNotFoundError``;
* the open-brackets set is populated from the position's bracket rows.

``alphamind.state.invocation_context`` is imported first to break the latent
circular import in ``state.repository.__init__`` (the repo helper module imports
``state.repository`` siblings transitively).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import alphamind.state.invocation_context  # noqa: F401 — break circular import seam
from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol
from alphamind._kernel.money import money, price
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
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
from alphamind.state.repository.position_state import (
    PositionStateNotFoundError,
    PositionStateSnapshot,
    load_position_state_at,
)
from alphamind.state.tables.brackets_codec import record_to_rows as bracket_record_to_rows
from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row

_T0 = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 2, 14, 0, 0, tzinfo=UTC)
_AS_OF = datetime(2026, 6, 3, 14, 0, 0, tzinfo=UTC)
_T_AFTER = datetime(2026, 6, 4, 14, 0, 0, tzinfo=UTC)


def _equity_fill(ts: datetime, fill_price: float, qty: float) -> PositionFill:
    return PositionFill(
        fill_timestamp=ts,
        fill_price=price(str(fill_price)),
        fill_quantity=qty,
        slippage=money("0"),
        fees=money("0"),
    )


def _equity_position(
    *,
    position_id: str,
    direction: Direction,
    share_count: float,
    avg_cost: float,
    fills: tuple[PositionFill, ...],
    bracket_id: str | None = None,
) -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=BracketId(bracket_id) if bracket_id is not None else None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=fills[0].fill_timestamp,
        details=EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=share_count,
            average_cost_basis_per_share=avg_cost,
            borrow_rate_pct=2.0 if direction is Direction.SHORT else None,
            accrued_borrow_cost_usd=0.0 if direction is Direction.SHORT else None,
            locate_status=LocateStatus.LOCATED if direction is Direction.SHORT else None,
            margin_held_usd=100.0 if direction is Direction.SHORT else None,
        ),
        execution_history=fills,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _option_position(
    *,
    position_id: str,
    contract_count: float,
    premium: float,
    fills: tuple[PositionFill, ...],
) -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=fills[0].fill_timestamp,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=200.0,
            expiration_date=date(2026, 7, 17),
            contract_type=OptionContractType.CALL,
            contract_count=contract_count,
            contract_multiplier=100.0,
            premium_paid_per_contract=premium,
            greeks=OptionGreeks(delta=0.5, gamma=0.1, theta=-0.1, vega=0.2),
        ),
        execution_history=fills,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(position_id: str) -> PositionRecord:
    leg_details = OptionsPositionDetails(
        underlying_ticker=Symbol("AAPL"),
        strike_price=200.0,
        expiration_date=date(2026, 7, 17),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=OptionGreeks(delta=0.5, gamma=0.1, theta=-0.1, vega=0.2),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING,
        direction=None,
        entry_timestamp=None,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_spread",
            legs=(StrategyLeg(leg_id="A", options=leg_details),),
            net_premium_usd=300.0,
            max_profit_usd=700.0,
            max_loss_usd=300.0,
            breakeven_levels=(205.0,),
            strategy_greeks=OptionGreeks(delta=0.5, gamma=0.1, theta=-0.1, vega=0.2),
        ),
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _bracket(bracket_id: str, position_id: str, entry_order_id: str) -> BracketRecord:
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId(entry_order_id),
        protective_legs=(
            BracketLeg(
                leg_id="L1",
                leg_type=BracketLegType.PRICE_STOP,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("AAPL"),
                    threshold_usd=90.0,
                    direction="LTE",
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
        ),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _persist_position(session: Session, record: PositionRecord) -> None:
    session.add(position_record_to_row(record))


def _persist_bracket(session: Session, record: BracketRecord) -> None:
    bracket_row, leg_rows = bracket_record_to_rows(record)
    session.add(bracket_row)
    session.flush()  # bracket_legs.bracket_id FK is non-deferrable
    for leg in leg_rows:
        session.add(leg)


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


class TestLoadPositionStateAt:
    def test_equity_no_prior_adds_matches_details(self, session: Session) -> None:
        record = _equity_position(
            position_id="POS-1",
            direction=Direction.LONG,
            share_count=50.0,
            avg_cost=100.0,
            fills=(_equity_fill(_T0, 100.0, 50.0),),
        )
        _persist_position(session, record)
        session.flush()

        snap = load_position_state_at(session, position_id=PositionId("POS-1"), as_of=_AS_OF)

        assert isinstance(snap, PositionStateSnapshot)
        assert snap.position_id == PositionId("POS-1")
        assert snap.direction == Direction.LONG
        assert snap.net_quantity_as_of == 50.0
        assert snap.average_cost_basis == price("100")
        assert snap.opened_at == _T0
        assert isinstance(snap.details, EquityPositionDetails)
        assert snap.open_brackets == ()

    def test_option_no_prior_adds_matches_details(self, session: Session) -> None:
        record = _option_position(
            position_id="POS-OPT",
            contract_count=4.0,
            premium=5.0,
            fills=(_equity_fill(_T0, 5.0, 4.0),),
        )
        _persist_position(session, record)
        session.flush()

        snap = load_position_state_at(session, position_id=PositionId("POS-OPT"), as_of=_AS_OF)

        assert isinstance(snap.details, OptionsPositionDetails)
        assert snap.net_quantity_as_of == 4.0
        assert snap.average_cost_basis == price("5")

    def test_short_position_net_quantity_is_signed(self, session: Session) -> None:
        record = _equity_position(
            position_id="POS-SHORT",
            direction=Direction.SHORT,
            share_count=30.0,
            avg_cost=50.0,
            fills=(_equity_fill(_T0, 50.0, 30.0),),
        )
        _persist_position(session, record)
        session.flush()

        snap = load_position_state_at(session, position_id=PositionId("POS-SHORT"), as_of=_AS_OF)

        assert snap.net_quantity_as_of == -30.0
        assert snap.average_cost_basis == price("50")

    def test_pre_as_of_add_folds_into_weighted_basis(self, session: Session) -> None:
        # Two long fills before as_of: 50@100 then 50@120 → net 100, weighted 110.
        record = _equity_position(
            position_id="POS-ADD",
            direction=Direction.LONG,
            share_count=100.0,
            avg_cost=110.0,
            fills=(_equity_fill(_T0, 100.0, 50.0), _equity_fill(_T1, 120.0, 50.0)),
        )
        _persist_position(session, record)
        session.flush()

        snap = load_position_state_at(session, position_id=PositionId("POS-ADD"), as_of=_AS_OF)

        assert snap.net_quantity_as_of == 100.0
        assert snap.average_cost_basis == price("110")
        assert snap.opened_at == _T0

    def test_fill_after_as_of_is_excluded(self, session: Session) -> None:
        # Second fill is AFTER as_of → only the first 50@100 counts.
        record = _equity_position(
            position_id="POS-LATE",
            direction=Direction.LONG,
            share_count=100.0,
            avg_cost=110.0,
            fills=(_equity_fill(_T0, 100.0, 50.0), _equity_fill(_T_AFTER, 120.0, 50.0)),
        )
        _persist_position(session, record)
        session.flush()

        snap = load_position_state_at(session, position_id=PositionId("POS-LATE"), as_of=_AS_OF)

        assert snap.net_quantity_as_of == 50.0
        assert snap.average_cost_basis == price("100")

    def test_missing_position_raises(self, session: Session) -> None:
        with pytest.raises(PositionStateNotFoundError):
            load_position_state_at(session, position_id=PositionId("NOPE"), as_of=_AS_OF)

    def test_no_fills_at_or_before_as_of_raises(self, session: Session) -> None:
        record = _equity_position(
            position_id="POS-FUTURE",
            direction=Direction.LONG,
            share_count=50.0,
            avg_cost=100.0,
            fills=(_equity_fill(_T_AFTER, 100.0, 50.0),),
        )
        _persist_position(session, record)
        session.flush()

        with pytest.raises(PositionStateNotFoundError):
            load_position_state_at(session, position_id=PositionId("POS-FUTURE"), as_of=_AS_OF)

    def test_multi_leg_strategy_raises(self, session: Session) -> None:
        _persist_position(session, _strategy_position("POS-MLEG"))
        session.flush()

        with pytest.raises(PositionStateNotFoundError):
            load_position_state_at(session, position_id=PositionId("POS-MLEG"), as_of=_AS_OF)

    def test_open_brackets_are_loaded(self, session: Session) -> None:
        from tests.state._fk_substrate import stub_order_row

        # Cyclic deferrable FKs: position ↔ bracket ↔ order land in one txn.
        record = _equity_position(
            position_id="POS-BR",
            direction=Direction.LONG,
            share_count=50.0,
            avg_cost=100.0,
            fills=(_equity_fill(_T0, 100.0, 50.0),),
            bracket_id="BR-1",
        )
        _persist_position(session, record)
        session.add(stub_order_row("ORD-1", "BR-1", position_id="POS-BR"))
        _persist_bracket(session, _bracket("BR-1", "POS-BR", "ORD-1"))
        session.flush()

        snap = load_position_state_at(session, position_id=PositionId("POS-BR"), as_of=_AS_OF)

        assert len(snap.open_brackets) == 1
        assert snap.open_brackets[0].bracket_id == BracketId("BR-1")
