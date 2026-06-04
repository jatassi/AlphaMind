"""Tests for ``OrderRow`` + round-trip codec (story 04c / ALP-360).

The SQL row schema is tightened to match ``OrderRecord``'s non-null shape:
``alpaca_order_id``, ``bracket_id``, and ``submission_timestamp`` are NOT NULL.
Pre-submission orders are not representable as ``OrderRecord`` and live in the
activity log / PM-decision provenance instead — they are not persisted at the
SQL layer.

Covers:
* Column shape + indexes + CHECK constraints (six enum-bound columns).
* Round-trip codec: every ``OrderRole`` x ``OrderDirection`` combination, every
  ``OrderClass``, every ``InstrumentSpec`` variant, and the Alpaca order-ID
  chain ordering.
* Nullable post-submission fields (``position_id``, ``avg_fill_price``) survive
  the round-trip.
* Alembic migration idempotency.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import (
    AlpacaOrderId,
    CommandId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import price
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records.orders import (
    EquityInstrumentSpec,
    InstrumentSpec,
    OptionsInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    StrategyInstrumentSpec,
)
from alphamind.portfolio_state.records.positions import InstrumentType, OptionContractType
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    record_to_row,
    row_to_record,
)

SUBMITTED_AT = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
LAST_UPDATE_AT = SUBMITTED_AT + timedelta(minutes=30)
EXP = date(2026, 6, 19)


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
# Record builders
# ---------------------------------------------------------------------------


def _equity_spec() -> EquityInstrumentSpec:
    return EquityInstrumentSpec(ticker=Symbol("AAPL"))


def _options_spec() -> OptionsInstrumentSpec:
    return OptionsInstrumentSpec(
        underlying=Symbol("AAPL"),
        strike=150.0,
        expiration=EXP,
        contract_type=OptionContractType.CALL,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
    )


def _strategy_spec() -> StrategyInstrumentSpec:
    return StrategyInstrumentSpec(
        legs=(
            _options_spec(),
            OptionsInstrumentSpec(
                underlying=Symbol("AAPL"),
                strike=160.0,
                expiration=EXP,
                contract_type=OptionContractType.CALL,
                contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            ),
        )
    )


def _market_order(**overrides: object) -> OrderRecord:
    """Build a valid post-submission MARKET ENTRY OrderRecord."""
    base: dict[str, Any] = {
        "order_id": "ord-1",
        "position_id": None,
        "bracket_id": "brk-1",
        "role": OrderRole.ENTRY,
        "instrument_spec": _equity_spec(),
        "direction": OrderDirection.BUY,
        "order_type": OrderType.MARKET,
        "order_class": OrderClass.SIMPLE,
        "price_parameters": PriceParameters(),
        "quantity": 10.0,
        "duration": OrderDuration.DAY,
        "status": OrderStatus.PENDING,
        "alpaca_order_id": "alp-1",
        "alpaca_order_id_chain": ("alp-1",),
        "submission_timestamp": SUBMITTED_AT,
        "last_update_timestamp": LAST_UPDATE_AT,
        "filled_quantity": 0.0,
        "avg_fill_price": None,
        "remaining_quantity": 10.0,
        "modification_count": 0,
        "originating_thesis_id": None,
        "originating_pm_command_id": None,
        "age_hours": 0.5,
    }
    base.update(overrides)
    return OrderRecord(**base)


# ---------------------------------------------------------------------------
# Table shape (AC #1)
# ---------------------------------------------------------------------------


class TestOrdersTableShape:
    def test_table_primary_key(self, engine: Engine) -> None:
        insp = inspect(engine)
        pk = insp.get_pk_constraint("orders")
        assert pk["constrained_columns"] == ["order_id"]

    def test_required_columns_are_not_null(self, engine: Engine) -> None:
        """Tightened-schema columns: bracket_id, submission_timestamp, chain_json."""
        insp = inspect(engine)
        cols = {c["name"]: c for c in insp.get_columns("orders")}
        # Per the OrderRecord shape: these are NOT NULL (post-submission only).
        assert cols["bracket_id"]["nullable"] is False
        assert cols["submission_timestamp"]["nullable"] is False
        assert cols["alpaca_order_id_chain_json"]["nullable"] is False
        # OrderRecord-nullable fields stay nullable; alpaca_order_id is nullable
        # (ALP-847: a monitor-enforced / un-routed leg carries no broker id).
        assert cols["alpaca_order_id"]["nullable"] is True
        assert cols["position_id"]["nullable"] is True
        assert cols["average_fill_price"]["nullable"] is True

    def test_table_has_expected_indexes(self, engine: Engine) -> None:
        insp = inspect(engine)
        idx_names = {idx["name"] for idx in insp.get_indexes("orders")}
        assert "ix_orders_status" in idx_names
        assert "ix_orders_position_id" in idx_names
        assert "ix_orders_bracket_id" in idx_names
        assert "ix_orders_alpaca_order_id" in idx_names

    @pytest.mark.parametrize(
        ("column", "bad_value"),
        [
            ("order_role", "BOGUS_ROLE"),
            ("order_class", "BOGUS_CLASS"),
            ("direction", "BOGUS_DIR"),
            ("order_type", "BOGUS_TYPE"),
            ("duration", "BOGUS_DUR"),
            ("status", "BOGUS_STATUS"),
        ],
    )
    def test_check_rejects_unknown_enum_value(
        self, session: Session, column: str, bad_value: str
    ) -> None:
        row = record_to_row(_market_order())
        setattr(row, column, bad_value)
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_client_order_id_unique_index_rejects_duplicate_non_null(
        self, session: Session
    ) -> None:
        """The ``ix_orders_client_order_id`` unique index admits one row per real
        ``client_order_id`` — two non-NULL rows sharing one raise IntegrityError
        (the durable-intent idempotency key 02a's fill resolution relies on).

        This constraint lost its dedicated coverage when 01a pruned the
        per-migration tests; the unique index survives in the squashed baseline.
        """
        from tests.state._fk_substrate import seed_position_cluster, stub_order_row

        seed_position_cluster(session)
        session.commit()

        coid = "inv-X.ENV-SA-1.0.0"
        session.add(stub_order_row("ord-dup-a", "bracket-1", client_order_id=coid))
        session.add(stub_order_row("ord-dup-b", "bracket-1", client_order_id=coid))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_client_order_id_unique_index_admits_multiple_nulls(self, session: Session) -> None:
        """Many NULL ``client_order_id`` rows coexist (SQLite treats NULLs as
        distinct) — protective-leg / never-dispatched orders stay NULL without
        colliding on the unique index."""
        from tests.state._fk_substrate import seed_position_cluster, stub_order_row

        seed_position_cluster(session)
        session.commit()

        session.add(stub_order_row("ord-null-a", "bracket-1", client_order_id=None))
        session.add(stub_order_row("ord-null-b", "bracket-1", client_order_id=None))
        session.commit()  # must not raise — both NULLs are admitted

        order_ids = {
            r.order_id
            for r in session.query(OrderRow).filter(OrderRow.client_order_id.is_(None)).all()
        }
        assert {"ord-null-a", "ord-null-b"} <= order_ids


# ---------------------------------------------------------------------------
# Round-trip codec (AC #2, #3, #5, #6)
# ---------------------------------------------------------------------------


class TestRoundTripCodec:
    def test_basic_market_entry_round_trips(self, session: Session) -> None:
        from tests.state._fk_substrate import (
            stub_bracket_row,
            stub_position_row,
        )

        record = _market_order()
        row = record_to_row(record)
        session.add(stub_position_row("stub-pos-1"))
        session.add(stub_bracket_row(row.bracket_id, "stub-pos-1", row.order_id))
        session.add(row)
        session.commit()

        readback = session.get(OrderRow, "ord-1")
        assert readback is not None
        assert row_to_record(readback) == record

    @pytest.mark.parametrize("role", list(OrderRole))
    @pytest.mark.parametrize("direction", list(OrderDirection))
    def test_role_and_direction_combinations_round_trip(
        self, role: OrderRole, direction: OrderDirection
    ) -> None:
        """AC #2 + #3: every (OrderRole, OrderDirection) pair round-trips faithfully."""
        record = _market_order(
            role=role, direction=direction, order_id=OrderId(f"ord-{role}-{direction}")
        )
        assert row_to_record(record_to_row(record)) == record

    @pytest.mark.parametrize(
        ("order_class", "instrument_spec"),
        [
            (OrderClass.SIMPLE, _equity_spec()),
            (OrderClass.BRACKET, _equity_spec()),
            (OrderClass.OCO, _equity_spec()),
            (OrderClass.OTO, _equity_spec()),
            (OrderClass.MLEG, _strategy_spec()),
        ],
    )
    def test_order_class_round_trips(
        self, order_class: OrderClass, instrument_spec: InstrumentSpec
    ) -> None:
        """AC #2: every OrderClass round-trips (MLEG paired with strategy spec).

        MLEG envelopes carry ``direction=None`` (ALP-614); the codec maps the
        record's ``None`` to a SQL ``NULL`` and back.
        """
        record = _market_order(
            order_class=order_class,
            instrument_spec=instrument_spec,
            order_id=OrderId(f"ord-{order_class}"),
            direction=None if order_class == OrderClass.MLEG else OrderDirection.BUY,
        )
        assert row_to_record(record_to_row(record)) == record

    def test_alpaca_chain_ordering_preserved(self) -> None:
        """AC #5: a 3-element chain remains a 3-element chain in the same order."""
        chain = ("alp-original", "alp-replaced-1", "alp-replaced-2")
        record = _market_order(
            alpaca_order_id=AlpacaOrderId("alp-replaced-2"),
            alpaca_order_id_chain=chain,
            modification_count=2,
        )
        readback = row_to_record(record_to_row(record))
        assert readback.alpaca_order_id_chain == chain
        assert readback.alpaca_order_id == "alp-replaced-2"

    @pytest.mark.parametrize(
        "instrument_spec",
        [_equity_spec(), _options_spec(), _strategy_spec()],
        ids=["equity", "options", "strategy"],
    )
    def test_instrument_spec_variants_round_trip(self, instrument_spec: InstrumentSpec) -> None:
        """AC #6: each InstrumentSpec discriminated-union variant round-trips."""
        # MLEG required when the spec is STRATEGY; SIMPLE otherwise.
        # MLEG envelopes carry ``direction=None`` (ALP-614).
        is_strategy = instrument_spec.instrument_type == InstrumentType.STRATEGY
        order_class = OrderClass.MLEG if is_strategy else OrderClass.SIMPLE
        record = _market_order(
            instrument_spec=instrument_spec,
            order_class=order_class,
            direction=None if is_strategy else OrderDirection.BUY,
        )
        readback = row_to_record(record_to_row(record))
        assert readback.instrument_spec == instrument_spec

    def test_nullable_post_submission_fields_round_trip(self) -> None:
        """OrderRecord-nullable fields (position_id, avg_fill_price) survive."""
        record = _market_order(position_id=None, avg_fill_price=None)
        readback = row_to_record(record_to_row(record))
        assert readback.position_id is None
        assert readback.avg_fill_price is None

    def test_client_order_id_round_trips(self) -> None:
        """ALP-836 — the durable-intent client_order_id survives the round-trip
        both set and unset (default None)."""
        from alphamind._kernel.ids import ClientOrderId

        with_coid = _market_order(
            status=OrderStatus.PENDING_SUBMIT,
            client_order_id=ClientOrderId("inv-X.ENV-SA-1.0.0"),
        )
        assert row_to_record(record_to_row(with_coid)) == with_coid
        assert record_to_row(with_coid).client_order_id == "inv-X.ENV-SA-1.0.0"
        # Default (protective-leg / never-dispatched) — stays None.
        assert row_to_record(record_to_row(_market_order())).client_order_id is None

    def test_partial_fill_with_chain_round_trips(self) -> None:
        """Spot-check from story verification: partially-filled active order, chain length 2."""
        record = _market_order(
            order_id=OrderId("ord-partial"),
            position_id=PositionId("pos-1"),
            order_type=OrderType.LIMIT,
            price_parameters=PriceParameters(limit_price=price("152.5"), stop_trigger_price=None),
            status=OrderStatus.PARTIALLY_FILLED,
            alpaca_order_id=AlpacaOrderId("alp-2"),
            alpaca_order_id_chain=("alp-1", "alp-2"),
            filled_quantity=4.0,
            avg_fill_price=152.7,
            remaining_quantity=6.0,
            modification_count=1,
            originating_thesis_id=ThesisId("thesis-7"),
            originating_pm_command_id=CommandId("cmd-12"),
        )
        assert row_to_record(record_to_row(record)) == record

    def test_fully_filled_then_cancelled_round_trips(self) -> None:
        """Spot-check: fully-filled cancelled-after-fill order."""
        record = _market_order(
            status=OrderStatus.CANCELLED,
            filled_quantity=10.0,
            avg_fill_price=149.85,
            remaining_quantity=0.0,
        )
        assert row_to_record(record_to_row(record)) == record

    def test_price_parameters_decoder_accepts_legacy_float_json(self) -> None:
        """Rows written by the pre-ALP-660 codec stored prices as JSON numbers.

        ``_price_parameters_from_json`` normalises both shapes via ``price()`` so
        production rows that pre-date this migration decode cleanly.
        """
        from alphamind.state.tables.orders_codec import _price_parameters_from_json

        legacy_payload = '{"limit_price": 152.5, "stop_trigger_price": null}'
        decoded = _price_parameters_from_json(legacy_payload)
        assert decoded.limit_price == price("152.5")
        assert decoded.stop_trigger_price is None

    def test_options_order_round_trips(self) -> None:
        """Spot-check: options order."""
        record = _market_order(
            order_id=OrderId("ord-opt"),
            instrument_spec=_options_spec(),
            direction=OrderDirection.BUY_TO_OPEN,
            quantity=2.0,
            remaining_quantity=2.0,
        )
        assert row_to_record(record_to_row(record)) == record

    def test_multi_leg_strategy_round_trips(self) -> None:
        """Spot-check: multi-leg strategy order persists with direction=None (ALP-614)."""
        record = _market_order(
            order_id=OrderId("ord-strat"),
            instrument_spec=_strategy_spec(),
            order_class=OrderClass.MLEG,
            direction=None,
            quantity=1.0,
            remaining_quantity=1.0,
        )
        readback = row_to_record(record_to_row(record))
        assert readback == record
        assert readback.direction is None

    def test_mleg_direction_persists_as_null(self) -> None:
        """The codec writes ``direction=None`` as a SQL NULL on the row (ALP-614)."""
        record = _market_order(
            order_id=OrderId("ord-mleg-null"),
            instrument_spec=_strategy_spec(),
            order_class=OrderClass.MLEG,
            direction=None,
        )
        row = record_to_row(record)
        assert row.direction is None
