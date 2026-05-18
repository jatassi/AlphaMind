"""Production ``OrderLookup`` / ``AdvLookup`` / ``VolLookup`` adapters (ALP-528).

These adapters fulfil the Protocols defined in :mod:`enrichment` against the
real persistence layer. The construction-site wiring in
``continuous_monitor/__main__.py`` instantiates them only on the paper-mode
branch and passes the resulting ``enrichment_callable`` into
:func:`run_fill_stream_consumer`.

Each adapter is small enough that the Protocol surface and the impl live
side-by-side; tests in ``tests/execution/paper_evaluation_harness/test_lookups.py``
exercise each adapter independently against an in-memory ORM substrate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.execution import OrderType as HarnessOrderType
from alphamind.execution.paper_evaluation_harness.enrichment import OrderAttributes
from alphamind.persistence.models import AssetUniverse
from alphamind.portfolio_state.records.orders import OrderDirection, OrderType
from alphamind.portfolio_state.records.positions import InstrumentType
from alphamind.risk_guardrails.guardrail_evaluation import RealizedVolEntry
from alphamind.state.tables.orders import OrderRow

__all__ = ["MapVolLookup", "SqlAdvLookup", "SqlOrderLookup"]


# OrderDirection has six variants (BUY, SELL, BUY_TO_OPEN, SELL_TO_OPEN,
# BUY_TO_CLOSE, SELL_TO_CLOSE). The harness only cares about buy-vs-sell for
# the live-adjusted-price sign and the fee dispatch, so the open/close
# discriminator collapses here.
_DIRECTION_TO_SIDE: Final[Mapping[OrderDirection, Literal["buy", "sell"]]] = {
    OrderDirection.BUY: "buy",
    OrderDirection.BUY_TO_OPEN: "buy",
    OrderDirection.BUY_TO_CLOSE: "buy",
    OrderDirection.SELL: "sell",
    OrderDirection.SELL_TO_OPEN: "sell",
    OrderDirection.SELL_TO_CLOSE: "sell",
}

# Portfolio-state OrderType has four variants (MARKET, LIMIT, STOP, STOP_LIMIT);
# the harness's ``PaperHarness.impact_coefficients`` keys are the three
# lowercase variants (market, limit, stop). STOP_LIMIT collapses to STOP — a
# stop-with-protective-limit shares the stop trigger's market-impact profile,
# and the limit cap doesn't change the per-share crossing cost the harness
# estimates.
_ORDER_TYPE_TO_HARNESS: Final[Mapping[OrderType, HarnessOrderType]] = {
    OrderType.MARKET: HarnessOrderType.market,
    OrderType.LIMIT: HarnessOrderType.limit,
    OrderType.STOP: HarnessOrderType.stop,
    OrderType.STOP_LIMIT: HarnessOrderType.stop,
}


class SqlOrderLookup:
    """Production ``OrderLookup`` impl backed by the ``orders`` table.

    Implementation note — the adapter projects the row into ``OrderAttributes``
    directly rather than rehydrating the full ``OrderRecord`` via the codec:
    the wedge only consumes four fields, and the codec round-trip would pull
    in price-parameter / quantity / Alpaca-chain validation that has no
    bearing on the harness's inputs.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_order_attributes(self, order_id: str) -> OrderAttributes | None:
        async with self._session_factory() as sess:
            row = (
                await sess.execute(select(OrderRow).where(OrderRow.order_id == order_id))
            ).scalar_one_or_none()
        if row is None:
            return None
        ticker_or_underlying = _extract_ticker(row.instrument_spec_json)
        if ticker_or_underlying is None:
            # STRATEGY parent orders (no single underlying) — the translator
            # filters STRATEGY parent fills upstream so this branch is unreachable
            # via the wedge. Return None defensively so a misuse surfaces as a
            # graceful no-estimate rather than an exception in the hot path.
            return None
        return OrderAttributes(
            order_type=_ORDER_TYPE_TO_HARNESS[OrderType(row.order_type)],
            side=_DIRECTION_TO_SIDE[OrderDirection(row.direction)],
            instrument_type=InstrumentType(json.loads(row.instrument_spec_json)["instrument_type"]),
            ticker_or_underlying=ticker_or_underlying,
        )


def _extract_ticker(instrument_spec_json: str) -> str | None:
    """Return the ticker (equity) / underlying (options) from an instrument spec.

    Returns ``None`` for STRATEGY specs, which have no single underlying and
    are filtered by the translator before reaching the wedge.
    """
    payload = json.loads(instrument_spec_json)
    kind = payload["instrument_type"]
    if kind == InstrumentType.EQUITY.value:
        return str(payload["ticker"])
    if kind == InstrumentType.OPTIONS.value:
        return str(payload["underlying"])
    return None


class SqlAdvLookup:
    """Production ``AdvLookup`` impl backed by ``asset_universe.avg_daily_volume_shares``.

    Returns ``None`` when the ticker is missing from the universe *or* when its
    ADV column is NULL — both cases route through the same no-estimate
    fallback in the harness.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_adv_shares(self, ticker: str) -> float | None:
        async with self._session_factory() as sess:
            row = (
                await sess.execute(
                    select(AssetUniverse.avg_daily_volume_shares).where(
                        AssetUniverse.ticker == ticker
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        (adv,) = row
        if adv is None:
            return None
        return float(adv)


class MapVolLookup:
    """Production ``VolLookup`` impl wrapping a ``Mapping[str, RealizedVolEntry]``.

    Sources the same per-underlying realized-vol map the breach-loop's
    ``FixtureIvProvider`` consumes. Today the production map is empty
    (constructed as ``FixtureIvProvider(surface={}, realized_vol={})`` in
    ``__main__.py``), so every fill flows through the None-fallback path; the
    real per-ticker realized-vol substrate is tracked by ALP-530.
    """

    def __init__(self, store: Mapping[str, RealizedVolEntry]) -> None:
        self._store = store

    async def get_realized_volatility(self, underlying: str) -> float | None:
        entry = self._store.get(underlying)
        if entry is None:
            return None
        return entry.trailing_30d_realized_vol
