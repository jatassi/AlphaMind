"""Tests for ``alphamind.scheduler.fresh_start`` (ALP-620).

Covers the cold-start bootstrap of the ``cash_ledger`` + ``drawdown_state``
singleton rows from an Alpaca account snapshot. The bootstrap is gated
behind ``--fresh-start`` on the scheduler CLI; this module exercises the
bootstrap function in isolation against a stubbed broker adapter, plus
the two hard-fail preconditions (positions present, cash_ledger already
initialized).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.money import money, price
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter.queries import (
    OrderSnapshot,
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.scheduler.fresh_start import (
    FreshStartPreconditionError,
    bootstrap_singletons_from_alpaca,
    run_fresh_start_bootstrap,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)
from tests.scheduler.conftest import _make_venue_config

_NOW = datetime(2026, 5, 25, 14, 30, 0, tzinfo=UTC)


def _make_account_snapshot(cash_usd: float = 100_000.0) -> TradeAccountSnapshot:
    money_val = money(cash_usd)
    return TradeAccountSnapshot(
        account_id="acc-fresh-start",
        cash=money_val,
        equity=money_val,
        buying_power=money_val,
        regt_buying_power=money_val,
        daytrading_buying_power=money_val,
        maintenance_margin=money(0.0),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


def _make_position_snapshot(symbol: str = "AAPL") -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=10.0,
        avg_entry_price=price(140.0),
        market_value=money(1500.0),
        cost_basis=money(1400.0),
        unrealized_pl=money(100.0),
        unrealized_plpc=0.0714,
        current_price=price(150.0),
        side="long",
    )


def _make_open_order_snapshot(symbol: str = "AAPL") -> OrderSnapshot:
    """A resting (open) order — a husk in order form on a non-flat account."""
    return OrderSnapshot(
        order_id="ord-open-1",
        client_order_id="cli-open-1",
        symbol=symbol,
        asset_class="us_equity",
        qty=10.0,
        filled_qty=0.0,
        filled_avg_price=None,
        side="buy",
        order_type="limit",
        time_in_force="day",
        order_class="simple",
        status="new",
        submitted_at=_NOW,
        filled_at=None,
        replaced_by=None,
        replaces=None,
        legs=None,
    )


class _StubQueries:
    """Sync stand-in for ``AccountStateQueries`` returning canned data.

    ``get_orders`` mirrors the production async-generator surface so the
    fresh-start open-orders precondition can consume ``status="open"`` the
    same way it consumes ``get_positions`` for the positions precondition.
    """

    def __init__(
        self,
        *,
        account: TradeAccountSnapshot,
        positions: tuple[PositionSnapshot, ...],
        open_orders: tuple[OrderSnapshot, ...] = (),
    ) -> None:
        self._account = account
        self._positions = positions
        self._open_orders = open_orders

    def get_account(self) -> TradeAccountSnapshot:
        return self._account

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        return self._positions

    def get_open_position(self, symbol: str) -> PositionSnapshot | None:
        # The fresh-start preconditions never consult the single-symbol read;
        # present to satisfy the AccountStateQueriesP protocol surface (ALP-943).
        return next((p for p in self._positions if p.symbol == symbol), None)

    async def get_orders(
        self,
        *,
        status: Literal["open", "closed", "all"] = "all",
        since: datetime | None = None,
        until: datetime | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> AsyncIterator[OrderSnapshot]:
        assert status == "open"
        for order in self._open_orders:
            yield order


class TestBootstrapSingletonsFromAlpaca:
    """Direct exercise of ``bootstrap_singletons_from_alpaca`` against a real session."""

    async def test_happy_path_inserts_both_singletons(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Empty Alpaca account → both singletons inserted with matching values."""
        async with async_factory() as session:
            await bootstrap_singletons_from_alpaca(
                session=session,
                account=_make_account_snapshot(100_000.0),
                positions=(),
                now=_NOW,
            )
            await session.commit()

        async with async_factory() as verify_session:
            cash_row = await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
            drawdown_row = await verify_session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)

        # The canonical singleton-codec format (``datetime_to_iso_z``) emits
        # the ``Z``-suffixed ISO form; the bootstrap matches so bootstrap-
        # written rows are byte-equal to canonical-codec writes.
        expected_iso = "2026-05-25T14:30:00+00:00".replace("+00:00", "Z")

        assert cash_row is not None
        assert cash_row.current_cash_usd == Decimal("100000.0")
        assert cash_row.settled_cash_usd == Decimal("100000.0")
        assert cash_row.available_buying_power_usd == Decimal("100000.0")
        assert cash_row.reserved_capital_usd == Decimal(0)
        assert cash_row.margin_held_usd == Decimal(0)
        assert cash_row.unsettled_proceeds_json == "[]"
        assert cash_row.last_updated_at == expected_iso

        assert drawdown_row is not None
        assert drawdown_row.equity_high_water_mark_usd == 100_000.0
        assert drawdown_row.current_drawdown_pct == 0.0
        assert drawdown_row.drawdown_duration_hours == 0.0
        assert drawdown_row.lifetime_max_drawdown_pct == 0.0
        assert drawdown_row.drawdown_by_source_json == "{}"
        # Both singletons share the same canonical timestamp form (no
        # cross-singleton format drift).
        assert drawdown_row.last_updated_at == expected_iso

    async def test_rejects_when_positions_present(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Any Alpaca-side position triggers ``FreshStartPreconditionError``."""
        async with async_factory() as session:
            with pytest.raises(FreshStartPreconditionError) as exc_info:
                await bootstrap_singletons_from_alpaca(
                    session=session,
                    account=_make_account_snapshot(),
                    positions=(_make_position_snapshot("AAPL"),),
                    now=_NOW,
                )

        message = str(exc_info.value)
        assert "AAPL" in message
        assert "open position" in message
        assert "--fresh-start" in message

        async with async_factory() as verify_session:
            # Neither singleton should have been inserted — the precondition
            # check runs before any write.
            assert (await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)) is None
            assert (await verify_session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)) is None

    async def test_rejects_when_open_orders_present(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Any resting open order triggers ``FreshStartPreconditionError``.

        A fresh account must be FLAT — positions empty *and* open orders empty
        (genesis-cutover runbook S6 / build-spec invariant 6). A resting
        bracket/entry order with no local Intent is the husk in order form, so
        the precondition refuses to bootstrap onto it.
        """
        async with async_factory() as session:
            with pytest.raises(FreshStartPreconditionError) as exc_info:
                await bootstrap_singletons_from_alpaca(
                    session=session,
                    account=_make_account_snapshot(),
                    positions=(),
                    open_orders=(_make_open_order_snapshot("AAPL"),),
                    now=_NOW,
                )

        message = str(exc_info.value)
        assert "AAPL" in message
        assert "open order" in message
        assert "--fresh-start" in message

        async with async_factory() as verify_session:
            # No singleton written — the precondition check precedes any write.
            assert (await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)) is None
            assert (await verify_session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)) is None

    async def test_flat_account_with_no_orders_passes(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A genuinely flat account (no positions, no open orders) bootstraps."""
        async with async_factory() as session:
            await bootstrap_singletons_from_alpaca(
                session=session,
                account=_make_account_snapshot(100_000.0),
                positions=(),
                open_orders=(),
                now=_NOW,
            )
            await session.commit()

        async with async_factory() as verify_session:
            assert (await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)) is not None

    async def test_rejects_when_cash_ledger_already_initialized(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Existing ``cash_ledger`` row triggers ``FreshStartPreconditionError``."""
        async with async_factory() as seed_session:
            seed_session.add(
                CashLedgerRow(
                    id=CASH_LEDGER_SINGLETON_ID,
                    current_cash_usd=Decimal("42000.00"),
                    settled_cash_usd=Decimal("42000.00"),
                    reserved_capital_usd=Decimal(0),
                    available_buying_power_usd=Decimal("42000.00"),
                    margin_held_usd=Decimal(0),
                    unsettled_proceeds_json="[]",
                    last_updated_at=_NOW.isoformat(),
                )
            )
            await seed_session.commit()

        async with async_factory() as session:
            with pytest.raises(FreshStartPreconditionError) as exc_info:
                await bootstrap_singletons_from_alpaca(
                    session=session,
                    account=_make_account_snapshot(),
                    positions=(),
                    now=_NOW,
                )

        message = str(exc_info.value)
        assert "already initialized" in message
        assert "42000" in message

        # The seeded value must still be there — the failed bootstrap must
        # not have touched the row.
        async with async_factory() as verify_session:
            existing = await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
            assert existing is not None
            assert existing.current_cash_usd == Decimal("42000.00")
            # And drawdown_state must remain absent — the seeded fixture
            # only inserted the cash row.
            assert (await verify_session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)) is None

    async def test_rejects_when_drawdown_state_already_initialized(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An asymmetric DB (drawdown_state populated, cash_ledger empty) still
        hard-fails the precondition rather than hitting an IntegrityError at
        commit. The two singletons share the same ``id='current'`` PK; a
        prior bootstrap or hand-seed that populated one but not the other
        must surface as a friendly ``FreshStartPreconditionError`` here, not
        as a SQLAlchemy traceback from the operator's perspective.
        """
        async with async_factory() as seed_session:
            seed_session.add(
                DrawdownStateRow(
                    id=DRAWDOWN_STATE_SINGLETON_ID,
                    equity_high_water_mark_usd=75_000.0,
                    current_drawdown_pct=0.0,
                    drawdown_duration_hours=0.0,
                    lifetime_max_drawdown_pct=0.0,
                    drawdown_by_source_json="{}",
                    last_updated_at=_NOW.isoformat(),
                )
            )
            await seed_session.commit()

        async with async_factory() as session:
            with pytest.raises(FreshStartPreconditionError) as exc_info:
                await bootstrap_singletons_from_alpaca(
                    session=session,
                    account=_make_account_snapshot(),
                    positions=(),
                    now=_NOW,
                )

        message = str(exc_info.value)
        assert "drawdown_state already initialized" in message
        assert "75000" in message

        async with async_factory() as verify_session:
            # The cash_ledger row must not have been inserted (precondition
            # bails before the writes).
            assert (await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)) is None
            # The seeded HWM must be unchanged.
            existing = await verify_session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
            assert existing is not None
            assert existing.equity_high_water_mark_usd == 75_000.0

    async def test_uses_full_decimal_precision_from_alpaca_cash(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Sub-cent precision in Alpaca's reported cash round-trips intact."""
        # Alpaca returns cash as a string the broker adapter parses via
        # ``money(raw_str)`` — a real reset may report e.g. "99999.97" rather
        # than a round 100k. Decimal must round-trip through DecimalText.
        precise_cash = money("99999.973")
        account = TradeAccountSnapshot(
            account_id="acc-precise",
            cash=precise_cash,
            equity=precise_cash,
            buying_power=precise_cash,
            regt_buying_power=precise_cash,
            daytrading_buying_power=precise_cash,
            maintenance_margin=money(0.0),
            daytrade_count=0,
            pattern_day_trader=False,
            status="ACTIVE",
        )

        async with async_factory() as session:
            await bootstrap_singletons_from_alpaca(
                session=session,
                account=account,
                positions=(),
                now=_NOW,
            )
            await session.commit()

        async with async_factory() as verify_session:
            cash_row = await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)

        assert cash_row is not None
        assert cash_row.current_cash_usd == Decimal("99999.973")


class TestRunFreshStartBootstrap:
    """Exercise the top-level entry that orchestrates broker fetch + write."""

    async def test_commits_singletons_via_stub_factory(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Stubbed ``account_queries_factory`` drives the bootstrap end-to-end."""
        account = _make_account_snapshot(50_000.0)
        captured_args: list[tuple[VenueConfig, ExecutionMode]] = []

        def _stub_factory(venue_config: VenueConfig, execution_mode: ExecutionMode) -> _StubQueries:
            captured_args.append((venue_config, execution_mode))
            return _StubQueries(account=account, positions=())

        await run_fresh_start_bootstrap(
            session_factory=async_factory,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
            now=_NOW,
            account_queries_factory=_stub_factory,
        )

        # The factory was constructed once with the venue/mode kwargs.
        assert len(captured_args) == 1
        assert captured_args[0][1] is ExecutionMode.paper

        async with async_factory() as verify_session:
            cash_row = await verify_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
            drawdown_row = await verify_session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == Decimal("50000.0")
        assert drawdown_row is not None
        assert drawdown_row.equity_high_water_mark_usd == 50_000.0

    async def test_propagates_precondition_error_without_writing(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Positions-present from stubbed broker → caller sees the precondition error."""
        positions = (_make_position_snapshot("AAPL"), _make_position_snapshot("MSFT"))

        def _stub_factory(venue_config: VenueConfig, execution_mode: ExecutionMode) -> _StubQueries:
            return _StubQueries(account=_make_account_snapshot(), positions=positions)

        with pytest.raises(FreshStartPreconditionError) as exc_info:
            await run_fresh_start_bootstrap(
                session_factory=async_factory,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                now=_NOW,
                account_queries_factory=_stub_factory,
            )

        # Both symbols surface in the error message (sorted) so the operator
        # immediately sees what's on the broker side.
        message = str(exc_info.value)
        assert "AAPL" in message
        assert "MSFT" in message

        async with async_factory() as verify_session:
            rows = (await verify_session.execute(select(CashLedgerRow))).scalars().all()
            assert rows == []

    async def test_propagates_open_orders_precondition_error(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Open orders from the stubbed broker abort the top-level bootstrap.

        Exercises ``run_fresh_start_bootstrap`` fetching open orders via the
        factory's ``get_orders(status="open")`` generator and refusing to
        bootstrap an account that is flat on positions but carries a resting
        order.
        """
        open_orders = (_make_open_order_snapshot("TSLA"),)

        def _stub_factory(venue_config: VenueConfig, execution_mode: ExecutionMode) -> _StubQueries:
            return _StubQueries(
                account=_make_account_snapshot(), positions=(), open_orders=open_orders
            )

        with pytest.raises(FreshStartPreconditionError) as exc_info:
            await run_fresh_start_bootstrap(
                session_factory=async_factory,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                now=_NOW,
                account_queries_factory=_stub_factory,
            )

        assert "TSLA" in str(exc_info.value)
        assert "open order" in str(exc_info.value)

        async with async_factory() as verify_session:
            rows = (await verify_session.execute(select(CashLedgerRow))).scalars().all()
            assert rows == []
