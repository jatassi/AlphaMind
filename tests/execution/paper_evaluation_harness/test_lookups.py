"""Tests for the production ``OrderLookup`` / ``AdvLookup`` / ``VolLookup``
adapters (ALP-528 — story 04a).

Each adapter is tested in isolation against a minimal in-memory substrate:

* ``SqlOrderLookup`` — on-disk SQLite engine with the full ORM schema, one
  seeded ``OrderRow`` per case. Verifies the row-to-``OrderAttributes``
  projection (instrument_type discriminator, direction → buy/sell, order_type
  enum mapping).
* ``SqlAdvLookup`` — same substrate, one seeded ``AssetUniverse`` row per
  case. Verifies the ``avg_daily_volume_shares`` projection (present /
  NULL / missing-ticker).
* ``MapVolLookup`` — pure in-memory ``Mapping[str, RealizedVolEntry]``;
  verifies the realized-vol projection on hit and the None-on-miss path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.execution import OrderType as HarnessOrderType
from alphamind.persistence.models import AssetUniverse, Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.positions import InstrumentType
from alphamind.risk_guardrails.guardrail_evaluation import RealizedVolEntry
from tests.state._fk_substrate import (
    stub_bracket_row,
    stub_order_row,
    stub_position_row,
    stub_thesis_row,
)


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401 — ensure ORM mappings are registered

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def _seed_order_with_attrs(
    db_path: str,
    *,
    order_id: str,
    instrument_spec_json: str,
    direction: str,
    order_type: str,
) -> None:
    """Insert a position/thesis/bracket/order cluster with the requested order
    attributes."""
    sync_engine = make_engine(db_path)
    with make_session_factory(sync_engine)() as sess:
        bracket_id = f"bracket-{order_id}"
        position_id = f"pos-{order_id}"
        thesis_id = f"thesis-{order_id}"
        sess.add(
            stub_position_row(position_id, thesis_id=thesis_id, bracket_id=bracket_id),
        )
        sess.add(stub_thesis_row(thesis_id, position_id))
        order = stub_order_row(order_id, bracket_id, position_id=position_id, direction=direction)
        # Override the stub's instrument_spec_json + order_type to match the case.
        order.instrument_spec_json = instrument_spec_json
        order.order_type = order_type
        sess.add(order)
        sess.add(stub_bracket_row(bracket_id, position_id, order_id))
        sess.commit()
    sync_engine.dispose()


# ---------------------------------------------------------------------------
# SqlOrderLookup
# ---------------------------------------------------------------------------


async def test_sql_order_lookup_returns_equity_buy_attrs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import SqlOrderLookup

    _seed_order_with_attrs(
        str(tmp_path / "alphamind.db"),
        order_id="order-equity-buy",
        instrument_spec_json=json.dumps({"instrument_type": "EQUITY", "ticker": "AAPL"}),
        direction="BUY",
        order_type="MARKET",
    )

    lookup = SqlOrderLookup(session_factory)
    attrs = await lookup.get_order_attributes("order-equity-buy")

    assert attrs is not None
    assert attrs.order_type is HarnessOrderType.market
    assert attrs.side == "buy"
    assert attrs.instrument_type is InstrumentType.EQUITY
    assert attrs.ticker_or_underlying == "AAPL"


async def test_sql_order_lookup_returns_none_when_order_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import SqlOrderLookup

    lookup = SqlOrderLookup(session_factory)
    attrs = await lookup.get_order_attributes("order-does-not-exist")
    assert attrs is None


async def test_sql_order_lookup_maps_sell_to_close_to_sell(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """``OrderDirection.SELL_TO_CLOSE`` normalises to harness side ``"sell"``."""
    from alphamind.execution.paper_evaluation_harness.lookups import SqlOrderLookup

    _seed_order_with_attrs(
        str(tmp_path / "alphamind.db"),
        order_id="order-sell-to-close",
        instrument_spec_json=json.dumps({"instrument_type": "EQUITY", "ticker": "AAPL"}),
        direction="SELL_TO_CLOSE",
        order_type="LIMIT",
    )

    lookup = SqlOrderLookup(session_factory)
    attrs = await lookup.get_order_attributes("order-sell-to-close")
    assert attrs is not None
    assert attrs.side == "sell"
    assert attrs.order_type is HarnessOrderType.limit


async def test_sql_order_lookup_strategy_spec_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """STRATEGY instrument_spec returns None (defensive wedge — no single underlying)."""
    from alphamind.execution.paper_evaluation_harness.lookups import SqlOrderLookup

    _seed_order_with_attrs(
        str(tmp_path / "alphamind.db"),
        order_id="order-strategy",
        instrument_spec_json=json.dumps({"instrument_type": InstrumentType.STRATEGY.value}),
        direction="BUY",
        order_type="MARKET",
    )

    lookup = SqlOrderLookup(session_factory)
    attrs = await lookup.get_order_attributes("order-strategy")
    assert attrs is None


async def test_sql_order_lookup_options_uses_underlying_ticker(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import SqlOrderLookup

    _seed_order_with_attrs(
        str(tmp_path / "alphamind.db"),
        order_id="order-options",
        instrument_spec_json=json.dumps(
            {
                "instrument_type": "OPTIONS",
                "underlying": "AAPL",
                "strike": 200.0,
                "expiration": "2026-06-19",
                "contract_type": "CALL",
                "contract_multiplier": 100.0,
            }
        ),
        direction="BUY_TO_OPEN",
        order_type="LIMIT",
    )

    lookup = SqlOrderLookup(session_factory)
    attrs = await lookup.get_order_attributes("order-options")
    assert attrs is not None
    assert attrs.instrument_type is InstrumentType.OPTIONS
    assert attrs.ticker_or_underlying == "AAPL"
    assert attrs.side == "buy"


# ---------------------------------------------------------------------------
# SqlAdvLookup
# ---------------------------------------------------------------------------


async def _seed_asset_universe(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ticker: str,
    adv: int | None,
) -> None:
    async with session_factory() as sess:
        sess.add(
            AssetUniverse(
                asset_id=f"asset-{ticker}",
                ticker=ticker,
                full_name=ticker,
                asset_class="EQUITY",
                asset_role="UNIVERSE",
                exchange="NASDAQ",
                cik=None,
                figi=None,
                isin=None,
                shares_outstanding=None,
                float_shares=None,
                market_cap_usd=None,
                avg_daily_volume_shares=adv,
                avg_daily_volume_notional_usd=None,
                beta_spy=None,
                analyst_count=None,
                options_chain_liquid=None,
                ipo_date=None,
                is_active=1,
                added_date="2024-01-01",
                removed_date=None,
                removal_reason=None,
                last_updated="2024-01-01",
            )
        )
        await sess.commit()


async def test_sql_adv_lookup_returns_adv_when_present(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import SqlAdvLookup

    await _seed_asset_universe(session_factory, ticker="AAPL", adv=50_000_000)
    lookup = SqlAdvLookup(session_factory)
    adv = await lookup.get_adv_shares("AAPL")
    assert adv == 50_000_000.0


async def test_sql_adv_lookup_returns_none_when_adv_null(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import SqlAdvLookup

    await _seed_asset_universe(session_factory, ticker="NULLVOL", adv=None)
    lookup = SqlAdvLookup(session_factory)
    adv = await lookup.get_adv_shares("NULLVOL")
    assert adv is None


async def test_sql_adv_lookup_returns_none_when_ticker_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import SqlAdvLookup

    lookup = SqlAdvLookup(session_factory)
    adv = await lookup.get_adv_shares("UNKNOWN")
    assert adv is None


# ---------------------------------------------------------------------------
# MapVolLookup
# ---------------------------------------------------------------------------


async def test_map_vol_lookup_returns_scalar_on_hit() -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import MapVolLookup

    store = {
        "AAPL": RealizedVolEntry(underlying="AAPL", trailing_30d_realized_vol=0.25),
    }
    lookup = MapVolLookup(store)
    vol = await lookup.get_realized_volatility("AAPL")
    assert vol == 0.25


async def test_map_vol_lookup_returns_none_on_miss() -> None:
    from alphamind.execution.paper_evaluation_harness.lookups import MapVolLookup

    lookup = MapVolLookup({})  # empty map — production default per parent decision H
    vol = await lookup.get_realized_volatility("AAPL")
    assert vol is None
