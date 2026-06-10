"""Tests for ``FillRecordRow`` + Pydantic ``FillRecord`` (story 05 / ALP-363).

The story defines an append-only Tier-2 lifecycle table: one row per fill event
with a ``processing_status`` flag (unprocessed / processed / quarantined). The
The fill collection fill-integration write path queries unprocessed fills, integrates them,
and marks them processed inside the same transaction that mutates positions /
orders / cash. The deduplication contract is enforced by a UNIQUE composite key
on ``(order_id, fill_timestamp, fill_quantity, fill_price)``.

Covers:
* Typed ``FillRecord`` Pydantic façade — round-trips faithfully through the SQL
  row, including the optional Reg T attribution + paper-mode live-execution
  estimate metadata.
* Column shape, indexes, CHECK constraints, FKs.
* ``append_fill_record`` write helper: inserts as ``unprocessed`` with a
  non-null ``persistence_timestamp``, idempotent on the dedupe key, rejects
  fills referencing a nonexistent ``order_id``.
* Application-level invariant: ``processing_status = 'processed'`` requires a
  non-null ``processing_invocation_id``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    Symbol,
)
from alphamind._kernel.money import Price, money, price, signed_money
from alphamind.execution.write_paths.fill_persistence import (
    append_fill_record,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    begin_write_immediate,
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.orders import (
    EquityInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import LiveExecutionEstimate
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
    RegTMarginAttribution,
)
from alphamind.state.tables.fill_records import (
    FillRecordRow,
)
from alphamind.state.tables.fill_records_codec import (
    record_to_row,
    row_to_record,
)
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)

FILL_AT = datetime(2026, 5, 7, 12, 15, 0, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
PERSISTED_AT = datetime(2026, 5, 7, 12, 15, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures (sync — table-shape tests)
# ---------------------------------------------------------------------------


def _process_lifetime() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/p/pip.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux",
    )


def _invocation_record(invocation_id: str = "inv-1") -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at="2026-05-07T14:30:00Z",
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/p/cfg.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/p/cal.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _order_record(order_id: str = "ord-1") -> OrderRecord:
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=None,
        bracket_id=BracketId("brk-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-1"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-1"),),
        submission_timestamp=SUBMITTED_AT,
        last_update_timestamp=SUBMITTED_AT + timedelta(seconds=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=0.5,
    )


def _fill_record(
    fill_id: str = "fill-1",
    order_id: str = "ord-1",
    fill_timestamp: datetime = FILL_AT,
    fill_quantity: float = 5.0,
    fill_price: Price | None = None,
    processing_status: FillProcessingStatus = FillProcessingStatus.UNPROCESSED,
    processing_invocation_id: str | None = None,
    processing_timestamp: datetime | None = None,
    regt_attribution: RegTMarginAttribution | None = None,
    live_execution_estimate: LiveExecutionEstimate | None = None,
    persistence_timestamp: datetime = PERSISTED_AT,
) -> FillRecord:
    if fill_price is None:
        fill_price = price(150.25)
    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        fill_timestamp=fill_timestamp,
        fill_price=price(fill_price),
        fill_quantity=fill_quantity,
        remaining_quantity_after=10.0 - fill_quantity,
        order_status_after=OrderStatus.PARTIALLY_FILLED,
        slippage_usd=signed_money(0.05),
        fees_usd=money(0.10),
        execution_venue="NASDAQ",
        gateway_reference="alp-1",
        persistence_timestamp=persistence_timestamp,
        processing_status=processing_status,
        processing_invocation_id=processing_invocation_id,
        processing_timestamp=processing_timestamp,
        regt_attribution=regt_attribution,
        live_execution_estimate=live_execution_estimate,
    )


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Sync in-memory SQLite engine with the full schema."""
    import alphamind.state.tables  # noqa: F401

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _seed_order_with_bracket(sess: Session) -> None:
    """Insert process-lifetime, invocation, and the order record with its bracket FK parent.

    orders.bracket_id references brackets.bracket_id (deferrable).
    brackets.position_id references positions.position_id (deferrable).
    brackets.entry_order_id references orders.order_id (deferrable).
    All land in one transaction so the deferred FKs resolve at COMMIT.
    """
    from tests.state._fk_substrate import (
        stub_bracket_row,
        stub_position_row,
    )

    order_row = order_record_to_row(_order_record())
    sess.add(stub_position_row("stub-pos-fill"))
    sess.add(order_row)
    sess.add(
        stub_bracket_row(
            order_row.bracket_id,
            "stub-pos-fill",
            order_row.order_id,
        )
    )


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """Sync session pre-seeded with one process-lifetime, invocation, and order row."""
    factory = make_session_factory(engine)
    with factory() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime()))
        sess.flush()
        sess.add(invocation_record_to_row(_invocation_record()))
        sess.flush()
        _seed_order_with_bracket(sess)
        sess.commit()
        yield sess


@pytest.fixture()
async def async_engine_and_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Async engine + factory backed by an on-disk SQLite DB; FK targets pre-seeded."""
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime()))
        sess.flush()
        sess.add(invocation_record_to_row(_invocation_record()))
        sess.flush()
        _seed_order_with_bracket(sess)
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Round-trip codec
# ---------------------------------------------------------------------------


class TestFillRecordRoundTrip:
    def test_basic_unprocessed_fill_round_trips(self, session: Session) -> None:
        record = _fill_record()
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(FillRecordRow, "fill-1")
        assert readback is not None
        assert row_to_record(readback) == record

    def test_processed_fill_round_trips_with_attribution(self, session: Session) -> None:
        attribution = RegTMarginAttribution(
            regt_margin_before=signed_money(10000.0),
            regt_margin_after=signed_money(11500.0),
            regt_marginal_consumption=signed_money(1500.0),
            pm_equivalent_before=signed_money(8000.0),
            pm_equivalent_after=signed_money(9000.0),
            pm_marginal_consumption=signed_money(1000.0),
            regt_excess_over_pm=signed_money(500.0),
            pm_model_version="ibkr_mirror_v1_2025Q3",
        )
        record = _fill_record(
            processing_status=FillProcessingStatus.PROCESSED,
            processing_invocation_id="inv-1",
            processing_timestamp=FILL_AT + timedelta(minutes=15),
            regt_attribution=attribution,
        )
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(FillRecordRow, "fill-1")
        assert readback is not None
        assert row_to_record(readback) == record

    def test_paper_mode_live_execution_estimate_round_trips(self, session: Session) -> None:
        estimate = LiveExecutionEstimate(
            estimated_spread_usd=money(0.02),
            estimated_impact_usd=money(0.05),
            estimated_regulatory_fees_usd=money(0.01),
            live_adjusted_fill_price=price(150.30),
        )
        record = _fill_record(live_execution_estimate=estimate)
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(FillRecordRow, "fill-1")
        assert readback is not None
        assert row_to_record(readback) == record

    def test_money_fields_preserve_decimal_precision_through_codec(self, session: Session) -> None:
        """ALP-462 — the codec layer is the durability boundary. ``Money`` and
        ``Price`` values must round-trip exactly through SQLite's ``Numeric``
        column type. This test pins the invariant: ``money("1234567.89")`` →
        encode → decode → equals the original Decimal exactly.
        """
        from decimal import Decimal

        from alphamind._kernel.money import money, price

        record = _fill_record(
            fill_price=price("1234567.89"),
            fill_quantity=2.0,
        ).model_copy(
            update={
                "fees_usd": money("0.07"),
                "slippage_usd": money("0.123456789012"),
            }
        )
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(FillRecordRow, "fill-1")
        assert readback is not None
        rehydrated = row_to_record(readback)
        # Decimal-exact equality — the whole point of the migration is that
        # ``money("1234567.89")`` survives the storage round-trip without any
        # binary-float drift sneaking into the durability layer.
        assert rehydrated.fill_price == price("1234567.89")
        assert rehydrated.fees_usd == money("0.07")
        assert rehydrated.slippage_usd == money("0.123456789012")
        # And the equality on the record itself round-trips.
        assert rehydrated == record
        # Sanity-check the underlying Decimal compare to confirm we didn't
        # silently lose precision through Pydantic float coercion.
        assert isinstance(rehydrated.fill_price, Decimal)
        assert isinstance(rehydrated.fees_usd, Decimal)


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestFillRecordsTableShape:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("fill_records")}
        assert cols == {
            "fill_id",
            "order_id",
            "fill_timestamp",
            "fill_price",
            "fill_quantity",
            "remaining_quantity_after",
            "order_status_after",
            "slippage_usd",
            "fees_usd",
            "execution_venue",
            "gateway_reference",
            "persistence_timestamp",
            "processing_status",
            "processing_invocation_id",
            "processing_timestamp",
            "regt_attribution_json",
            "live_execution_estimate_json",
        }
        pk = insp.get_pk_constraint("fill_records")
        assert pk["constrained_columns"] == ["fill_id"]

    def test_indexes_present(self, engine: Engine) -> None:
        insp = inspect(engine)
        names = {idx["name"] for idx in insp.get_indexes("fill_records")}
        assert "ix_fill_records_processing_status" in names
        assert "ix_fill_records_order_id" in names

    def test_unique_dedupe_constraint_present(self, engine: Engine) -> None:
        insp = inspect(engine)
        uniques = insp.get_unique_constraints("fill_records")
        match = next((u for u in uniques if u["name"] == "uq_fill_records_dedupe"), None)
        assert match is not None
        assert match["column_names"] == [
            "order_id",
            "fill_timestamp",
            "fill_quantity",
            "fill_price",
        ]

    def test_foreign_keys_present(self, engine: Engine) -> None:
        insp = inspect(engine)
        fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in insp.get_foreign_keys("fill_records")
        }
        assert (("order_id",), "orders") in fks
        assert (("processing_invocation_id",), "invocations") in fks

    def test_check_rejects_unknown_processing_status(self, session: Session) -> None:
        row = record_to_row(_fill_record())
        row.processing_status = "BOGUS"
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_order_status_after(self, session: Session) -> None:
        row = record_to_row(_fill_record())
        row.order_status_after = "BOGUS"
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_fk_rejects_nonexistent_order_id(self, session: Session) -> None:
        row = record_to_row(_fill_record(order_id=OrderId("ord-missing")))
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Application-level invariant
# ---------------------------------------------------------------------------


class TestProcessedRequiresInvocationId:
    def test_processed_without_invocation_id_raises(self) -> None:
        with pytest.raises(ValueError, match="processing_invocation_id"):
            _fill_record(
                processing_status=FillProcessingStatus.PROCESSED,
                processing_invocation_id=None,
                processing_timestamp=FILL_AT,
            )

    def test_processed_without_processing_timestamp_raises(self) -> None:
        with pytest.raises(ValueError, match="processing_timestamp"):
            _fill_record(
                processing_status=FillProcessingStatus.PROCESSED,
                processing_invocation_id="inv-1",
                processing_timestamp=None,
            )

    def test_unprocessed_with_invocation_id_raises(self) -> None:
        with pytest.raises(ValueError, match="unprocessed"):
            _fill_record(
                processing_status=FillProcessingStatus.UNPROCESSED,
                processing_invocation_id="inv-1",
                processing_timestamp=None,
            )


# ---------------------------------------------------------------------------
# append_fill_record helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAppendFillRecord:
    async def test_inserts_unprocessed_with_persistence_timestamp(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        record = _fill_record()
        async with factory() as session:
            await begin_write_immediate(session)
            await append_fill_record(session, record)
            await session.commit()
            rows = (await session.execute(select(FillRecordRow))).scalars().all()
        assert len(rows) == 1
        readback = rows[0]
        assert readback.processing_status == FillProcessingStatus.UNPROCESSED.value
        assert readback.persistence_timestamp != ""

    async def test_idempotent_on_dedupe_key(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        record = _fill_record()
        async with factory() as session:
            await begin_write_immediate(session)
            await append_fill_record(session, record)
            await session.commit()
        async with factory() as session:
            await begin_write_immediate(session)
            await append_fill_record(session, record)
            await session.commit()
        async with factory() as session:
            rows = (await session.execute(select(FillRecordRow))).scalars().all()
        assert len(rows) == 1

    async def test_idempotent_skip_preserves_first_persistence_timestamp(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Second append with same dedupe key does NOT overwrite first row."""
        _, factory = async_engine_and_factory
        first = _fill_record(fill_id="fill-1", persistence_timestamp=PERSISTED_AT)
        second = _fill_record(
            fill_id="fill-2-different-id-same-dedupe-key",
            persistence_timestamp=PERSISTED_AT + timedelta(hours=1),
        )
        async with factory() as session:
            await begin_write_immediate(session)
            await append_fill_record(session, first)
            await session.commit()
        async with factory() as session:
            await begin_write_immediate(session)
            await append_fill_record(session, second)
            await session.commit()
        async with factory() as session:
            rows = (await session.execute(select(FillRecordRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].fill_id == "fill-1"

    async def test_insert_rejects_nonexistent_order_id(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        record = _fill_record(order_id=OrderId("ord-missing"))
        async with factory() as session:
            await begin_write_immediate(session)
            with pytest.raises(IntegrityError):
                await append_fill_record(session, record)
                await session.commit()


# Avoid unused-import lint when only date is referenced via _order_record's expiration field
_ = date
