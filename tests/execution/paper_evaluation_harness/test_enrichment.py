"""Tests for ``attach_live_execution_estimate`` (ALP-528 — story 04a).

The wedge takes a ``FillRecord`` (translator output) and three lookup adapters,
calls :func:`compute_live_execution_estimate`, and returns a new ``FillRecord``
with ``live_execution_estimate`` populated. When any required input is
unavailable (vol, ADV, missing order, harness returns None), the wedge returns
the record unchanged.

The three lookup Protocols (``OrderLookup``, ``AdvLookup``, ``VolLookup``)
support in-memory fakes so unit tests stay free of DB I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import pytest

from alphamind._kernel.money import money, price
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.portfolio_state.records.positions import InstrumentType
from alphamind.state.records import FillProcessingStatus, FillRecord


# ---------------------------------------------------------------------------
# Config / record fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        cat_per_executed_share=0.000166,
        taf_per_share_sells=0.000145,
        sec_pct_of_notional_sells=0.0000080,
        orf_per_options_contract=0.02188,
        occ_per_options_contract=0.02,
    )


@pytest.fixture()
def harness_config(fee_schedule: FeeSchedule) -> PaperHarness:
    return PaperHarness(
        spread_buffer_pct=10,
        impact_coefficients={
            OrderType.market: 0.5,
            OrderType.limit: 0.25,
            OrderType.stop: 0.75,
        },
        fee_schedule=fee_schedule,
    )


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _equity_fill_record(
    *,
    order_id: str = "order-1",
    fill_price_value: str = "200",
    fill_quantity: float = 1000.0,
) -> FillRecord:
    return FillRecord(
        fill_id="fill-test-1",
        order_id=order_id,
        fill_timestamp=_now_utc(),
        fill_price=price(fill_price_value),
        fill_quantity=fill_quantity,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=None,
        fees_usd=money("0"),
        execution_venue="paper",
        gateway_reference="alp-test-1",
        persistence_timestamp=_now_utc(),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


# ---------------------------------------------------------------------------
# In-memory fakes for the lookup Protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OrderAttrs:
    """Shape the OrderLookup Protocol returns; must match production contract."""

    order_type: OrderType
    side: Literal["buy", "sell"]
    instrument_type: InstrumentType
    ticker_or_underlying: str


class _FakeOrderLookup:
    def __init__(self, table: Mapping[str, _OrderAttrs]) -> None:
        self._table = dict(table)

    async def get_order_attributes(self, order_id: str) -> _OrderAttrs | None:
        return self._table.get(order_id)


class _FakeAdvLookup:
    def __init__(self, table: Mapping[str, float | None]) -> None:
        self._table = dict(table)

    async def get_adv_shares(self, ticker: str) -> float | None:
        return self._table.get(ticker)


class _FakeVolLookup:
    def __init__(self, table: Mapping[str, float | None]) -> None:
        self._table = dict(table)

    async def get_realized_volatility(self, underlying: str) -> float | None:
        return self._table.get(underlying)


# ---------------------------------------------------------------------------
# Tracer bullet: equity buy with all inputs → record carries populated estimate
# with live_adjusted_fill_price > fill_price.
# ---------------------------------------------------------------------------


async def test_equity_buy_populates_estimate_with_higher_live_price(
    harness_config: PaperHarness,
) -> None:
    from alphamind.execution.paper_evaluation_harness import attach_live_execution_estimate

    record = _equity_fill_record(order_id="order-1", fill_price_value="200", fill_quantity=1000.0)
    order_lookup = _FakeOrderLookup(
        {
            "order-1": _OrderAttrs(
                order_type=OrderType.market,
                side="buy",
                instrument_type=InstrumentType.EQUITY,
                ticker_or_underlying="AAPL",
            )
        }
    )
    adv_lookup = _FakeAdvLookup({"AAPL": 50_000_000.0})
    vol_lookup = _FakeVolLookup({"AAPL": 0.20})

    result = await attach_live_execution_estimate(
        record,
        order_lookup=order_lookup,
        adv_lookup=adv_lookup,
        vol_lookup=vol_lookup,
        config=harness_config,
    )

    assert result.live_execution_estimate is not None
    assert result.live_execution_estimate.live_adjusted_fill_price > record.fill_price
    # The other fields on the record are unchanged.
    assert result.fill_id == record.fill_id
    assert result.order_id == record.order_id
    assert result.fill_price == record.fill_price


# ---------------------------------------------------------------------------
# Sign convention: equity sell → live_adjusted_fill_price < fill_price.
# ---------------------------------------------------------------------------


async def test_equity_sell_yields_lower_live_price(harness_config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import attach_live_execution_estimate

    record = _equity_fill_record(order_id="order-sell", fill_price_value="200", fill_quantity=1000.0)
    order_lookup = _FakeOrderLookup(
        {
            "order-sell": _OrderAttrs(
                order_type=OrderType.market,
                side="sell",
                instrument_type=InstrumentType.EQUITY,
                ticker_or_underlying="AAPL",
            )
        }
    )
    adv_lookup = _FakeAdvLookup({"AAPL": 50_000_000.0})
    vol_lookup = _FakeVolLookup({"AAPL": 0.20})

    result = await attach_live_execution_estimate(
        record,
        order_lookup=order_lookup,
        adv_lookup=adv_lookup,
        vol_lookup=vol_lookup,
        config=harness_config,
    )

    assert result.live_execution_estimate is not None
    assert result.live_execution_estimate.live_adjusted_fill_price < record.fill_price


# ---------------------------------------------------------------------------
# ADV None → record unchanged.
# ---------------------------------------------------------------------------


async def test_adv_none_returns_record_unchanged(harness_config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import attach_live_execution_estimate

    record = _equity_fill_record(order_id="order-1")
    order_lookup = _FakeOrderLookup(
        {
            "order-1": _OrderAttrs(
                order_type=OrderType.market,
                side="buy",
                instrument_type=InstrumentType.EQUITY,
                ticker_or_underlying="AAPL",
            )
        }
    )
    adv_lookup = _FakeAdvLookup({"AAPL": None})
    vol_lookup = _FakeVolLookup({"AAPL": 0.20})

    result = await attach_live_execution_estimate(
        record,
        order_lookup=order_lookup,
        adv_lookup=adv_lookup,
        vol_lookup=vol_lookup,
        config=harness_config,
    )
    assert result.live_execution_estimate is None
    assert result == record


# ---------------------------------------------------------------------------
# Vol None → record unchanged.
# ---------------------------------------------------------------------------


async def test_vol_none_returns_record_unchanged(harness_config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import attach_live_execution_estimate

    record = _equity_fill_record(order_id="order-1")
    order_lookup = _FakeOrderLookup(
        {
            "order-1": _OrderAttrs(
                order_type=OrderType.market,
                side="buy",
                instrument_type=InstrumentType.EQUITY,
                ticker_or_underlying="AAPL",
            )
        }
    )
    adv_lookup = _FakeAdvLookup({"AAPL": 50_000_000.0})
    vol_lookup = _FakeVolLookup({"AAPL": None})

    result = await attach_live_execution_estimate(
        record,
        order_lookup=order_lookup,
        adv_lookup=adv_lookup,
        vol_lookup=vol_lookup,
        config=harness_config,
    )
    assert result.live_execution_estimate is None
    assert result == record


# ---------------------------------------------------------------------------
# Order-not-found → record unchanged, WARNING logged.
# ---------------------------------------------------------------------------


async def test_order_not_found_returns_record_unchanged_and_logs_warning(
    harness_config: PaperHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from alphamind.execution.paper_evaluation_harness import attach_live_execution_estimate

    record = _equity_fill_record(order_id="order-missing")
    order_lookup = _FakeOrderLookup({})  # empty — no order maps to "order-missing"
    adv_lookup = _FakeAdvLookup({"AAPL": 50_000_000.0})
    vol_lookup = _FakeVolLookup({"AAPL": 0.20})

    with caplog.at_level(logging.WARNING, logger="alphamind.execution.paper_evaluation_harness"):
        result = await attach_live_execution_estimate(
            record,
            order_lookup=order_lookup,
            adv_lookup=adv_lookup,
            vol_lookup=vol_lookup,
            config=harness_config,
        )

    assert result == record
    assert any(
        rec.levelno == logging.WARNING and "order-missing" in rec.getMessage()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Options buy with all inputs → record carries CAT + ORF + OCC fees only.
# ---------------------------------------------------------------------------


async def test_options_buy_populates_estimate_with_options_fees(
    harness_config: PaperHarness, fee_schedule: FeeSchedule
) -> None:
    from alphamind.execution.paper_evaluation_harness import attach_live_execution_estimate

    record = _equity_fill_record(
        order_id="order-opt", fill_price_value="5.00", fill_quantity=10.0
    )
    order_lookup = _FakeOrderLookup(
        {
            "order-opt": _OrderAttrs(
                order_type=OrderType.market,
                side="buy",
                instrument_type=InstrumentType.OPTIONS,
                ticker_or_underlying="AAPL",
            )
        }
    )
    # For options, "volume" is contract-level ADV; we still feed it from the
    # adv_lookup keyed by the underlying — same key the equity path uses.
    adv_lookup = _FakeAdvLookup({"AAPL": 1_000_000.0})
    vol_lookup = _FakeVolLookup({"AAPL": 0.30})

    result = await attach_live_execution_estimate(
        record,
        order_lookup=order_lookup,
        adv_lookup=adv_lookup,
        vol_lookup=vol_lookup,
        config=harness_config,
    )

    assert result.live_execution_estimate is not None
    # Options buy regulatory fees = CAT + ORF + OCC (no SEC, no TAF — sells only).
    contracts = 10
    expected_fees = (
        fee_schedule.cat_per_executed_share * contracts * 100
        + fee_schedule.orf_per_options_contract * contracts
        + fee_schedule.occ_per_options_contract * contracts
    )
    assert float(result.live_execution_estimate.estimated_regulatory_fees_usd) == pytest.approx(
        expected_fees
    )


# ---------------------------------------------------------------------------
# Options buy with volume lookup returning None → record unchanged.
# ---------------------------------------------------------------------------


async def test_options_volume_none_returns_record_unchanged(
    harness_config: PaperHarness,
) -> None:
    from alphamind.execution.paper_evaluation_harness import attach_live_execution_estimate

    record = _equity_fill_record(
        order_id="order-opt", fill_price_value="5.00", fill_quantity=10.0
    )
    order_lookup = _FakeOrderLookup(
        {
            "order-opt": _OrderAttrs(
                order_type=OrderType.market,
                side="buy",
                instrument_type=InstrumentType.OPTIONS,
                ticker_or_underlying="AAPL",
            )
        }
    )
    adv_lookup = _FakeAdvLookup({"AAPL": None})
    vol_lookup = _FakeVolLookup({"AAPL": 0.30})

    result = await attach_live_execution_estimate(
        record,
        order_lookup=order_lookup,
        adv_lookup=adv_lookup,
        vol_lookup=vol_lookup,
        config=harness_config,
    )
    assert result.live_execution_estimate is None
    assert result == record
