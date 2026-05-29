"""Tests for marketable-limit entry pricing — ALP-738.

An analyst "enter-now" equity entry (``type=limit`` with no ``entry_window``)
must be rewritten from its static away-from-market limit into a *marketable*
limit priced through the touch, so a working short thesis fills at the prevailing
quote instead of resting above a falling market (the 2026-05-28 zero-fill
incident). Patient-retest entries (a limit carrying an ``entry_window``) and
non-equity / market / stop_limit entries are left verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from alphamind._kernel.ids import PositionId
from alphamind._kernel.money import Price, money, price
from alphamind.commands.command_models import (
    AddCommand,
    BracketOrderParameters,
    Direction,
    EntryOrder,
    EntryWindow,
    EquityInstrument,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
    ThesisComponent,
)
from alphamind.execution.broker_adapter.entry_pricing import (
    TouchQuote,
    marketable_limit_price,
    rewrite_enter_now_entries,
)


class _FakeQuoteSource:
    """In-process QuoteSource returning a fixed per-symbol touch (or None)."""

    def __init__(self, quotes: dict[str, TouchQuote | None]) -> None:
        self._quotes = quotes
        self.requested: list[str] = []

    async def latest_quote(self, symbol: str) -> TouchQuote | None:
        self.requested.append(symbol)
        return self._quotes.get(symbol)


# Module-level singletons so they can be used as default args (ruff B008).
_DEFAULT_STOP_TRIGGER: Price = price(999.0)
_DEFAULT_TARGET_PRICE: Price = price(80.0)


def _hard_leg(ticker: str, trigger: Price = _DEFAULT_STOP_TRIGGER) -> PriceLeg:
    return PriceLeg(
        type="price",
        is_hard=True,
        condition=PriceCondition(underlying_trigger=ticker, comparator=">=", trigger_price=trigger),
        order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
    )


def _thesis(ticker: str) -> Thesis:
    return Thesis(
        summary=f"Short {ticker} on momentum break.",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference=ticker,
                narrative="Momentum rolling over.",
                key_assumptions=("Trend continues.",),
            ),
        ),
    )


def _equity_open(
    *,
    ticker: str = "SCHW",
    direction: Direction = "short",
    entry_order: EntryOrder,
    entry_window: EntryWindow | None = None,
    target_price: Price = _DEFAULT_TARGET_PRICE,
    stop_trigger: Price = _DEFAULT_STOP_TRIGGER,
) -> OpenCommand:
    # Defaults form a coherent SHORT bracket (take-profit 80 below entry, stop
    # 999 above). Long-direction callers pass coherent long geometry
    # (take-profit above entry, stop below) so the rewrite's geometry guard
    # does not skip them.
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=ticker, direction=direction),
        entry_order=entry_order,
        position_size=PositionSize(quantity=25.0, dollar_value=money(2_000.0)),
        target=Target(
            target_type="absolute_price",
            price=target_price,
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(_hard_leg(ticker, stop_trigger),),
        thesis=_thesis(ticker),
        entry_window=entry_window,
    )


_BID_ASK = TouchQuote(bid=price("85.10"), ask=price("85.14"))


@pytest.mark.asyncio
async def test_enter_now_short_rewritten_to_marketable_limit() -> None:
    """SCHW replay: a limit short with no entry_window becomes a marketable SELL limit.

    The analyst's static limit 85.5 sat above the RTH-open 85.12 and never
    filled; the marketable limit prices at/below the live bid so it crosses.
    """
    command = _equity_open(entry_order=EntryOrder(type="limit", limit_price=price("85.5")))
    source = _FakeQuoteSource({"SCHW": _BID_ASK})

    (rewritten,) = await rewrite_enter_now_entries(
        (command,), quote_source=source, bps_through_touch=5.0
    )

    assert isinstance(rewritten, OpenCommand)
    assert rewritten.entry_order.type == "limit"
    assert rewritten.entry_order.limit_price == price("85.05")
    assert Decimal(rewritten.entry_order.limit_price) <= Decimal(_BID_ASK.bid)
    # Everything else preserved.
    assert rewritten.instrument == command.instrument
    assert rewritten.target == command.target
    assert source.requested == ["SCHW"]


@pytest.mark.asyncio
async def test_long_enter_now_rewritten_to_marketable_buy_limit() -> None:
    # Coherent long bracket: stop (80) below entry, take-profit (90) above.
    command = _equity_open(
        ticker="AAPL",
        direction="long",
        entry_order=EntryOrder(type="limit", limit_price=price("80.0")),
        target_price=price("90.0"),
        stop_trigger=price("80.0"),
    )
    source = _FakeQuoteSource({"AAPL": _BID_ASK})

    (rewritten,) = await rewrite_enter_now_entries(
        (command,), quote_source=source, bps_through_touch=5.0
    )

    assert isinstance(rewritten, OpenCommand)
    assert rewritten.entry_order.limit_price == price("85.19")
    assert Decimal(rewritten.entry_order.limit_price) >= Decimal(_BID_ASK.ask)


@pytest.mark.asyncio
async def test_patient_retest_with_entry_window_left_verbatim() -> None:
    """ZS replay: a limit short carrying an entry_window is a patient retest, untouched."""
    window = EntryWindow(
        deadline=datetime(2026, 5, 29, 17, 30, tzinfo=UTC),
        decay_type="gradual",
        rationale="Short the relief bounce; overnight gap retest.",
    )
    command = _equity_open(
        ticker="ZS",
        entry_order=EntryOrder(type="limit", limit_price=price("137.0")),
        entry_window=window,
    )
    source = _FakeQuoteSource({"ZS": _BID_ASK})

    (rewritten,) = await rewrite_enter_now_entries(
        (command,), quote_source=source, bps_through_touch=5.0
    )

    assert rewritten is command
    assert source.requested == []  # no quote fetched for a patient entry


@pytest.mark.asyncio
async def test_market_and_stop_limit_entries_left_verbatim() -> None:
    market = _equity_open(entry_order=EntryOrder(type="market", limit_price=None))
    stop_limit = _equity_open(
        ticker="MSFT",
        entry_order=EntryOrder(
            type="stop_limit", limit_price=price("90.0"), stop_price=price("89.0")
        ),
    )
    source = _FakeQuoteSource({"SCHW": _BID_ASK, "MSFT": _BID_ASK})

    rewritten = await rewrite_enter_now_entries(
        (market, stop_limit), quote_source=source, bps_through_touch=5.0
    )

    assert rewritten[0] is market
    assert rewritten[1] is stop_limit
    assert source.requested == []


@pytest.mark.asyncio
async def test_non_equity_entry_left_verbatim() -> None:
    option = OpenCommand(
        command_type="open",
        instrument=OptionInstrument(
            asset_type="option",
            underlying="NVDA",
            strike=price(800.0),
            expiration="2026-06-19",
            contract_type="put",
            direction="long",
        ),
        entry_order=EntryOrder(type="limit", limit_price=price("12.0")),
        position_size=PositionSize(quantity=1.0, dollar_value=money(1_200.0)),
        target=Target(
            target_type="absolute_price",
            price=price(20.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(_hard_leg("NVDA"),),
        thesis=_thesis("NVDA"),
    )
    source = _FakeQuoteSource({"NVDA": _BID_ASK})

    (rewritten,) = await rewrite_enter_now_entries(
        (option,), quote_source=source, bps_through_touch=5.0
    )

    assert rewritten is option
    assert source.requested == []


@pytest.mark.asyncio
async def test_missing_quote_leaves_enter_now_entry_verbatim() -> None:
    """No live quote -> leave the analyst's limit (caught downstream by ALP-739)."""
    command = _equity_open(entry_order=EntryOrder(type="limit", limit_price=price("85.5")))
    source = _FakeQuoteSource({"SCHW": None})

    (rewritten,) = await rewrite_enter_now_entries(
        (command,), quote_source=source, bps_through_touch=5.0
    )

    assert rewritten is command
    assert source.requested == ["SCHW"]


@pytest.mark.asyncio
async def test_enter_now_skipped_when_marketable_would_invert_bracket() -> None:
    """If the marketable limit would cross the take-profit, leave verbatim.

    The default short bracket has take_profit=80.0 (cover below entry). A bid
    that prices the marketable SELL limit below 80 would invert the bracket
    (entry must stay above the take-profit for a short), which the broker would
    reject — so the rewrite is skipped and the analyst's entry is preserved.
    """
    command = _equity_open(entry_order=EntryOrder(type="limit", limit_price=price("82.0")))
    # bid 79.50 -> marketable 79.46 < take_profit 80.0 -> would invert.
    source = _FakeQuoteSource({"SCHW": TouchQuote(bid=price("79.50"), ask=price("79.54"))})

    (rewritten,) = await rewrite_enter_now_entries(
        (command,), quote_source=source, bps_through_touch=5.0
    )

    assert rewritten is command
    assert source.requested == ["SCHW"]


@pytest.mark.asyncio
async def test_add_command_left_verbatim() -> None:
    """ADD is out of scope — only OpenCommand entries are re-priced."""
    add = AddCommand(
        command_type="add",
        position_id=PositionId("POS-SCHW-001"),
        additional_quantity=10.0,
        additional_dollar_value=money(1_000.0),
        entry_order=EntryOrder(type="limit", limit_price=price("85.5")),
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="add",
            instrument_reference="SCHW",
            narrative="Add on confirmation.",
            key_assumptions=("Trend intact.",),
        ),
        bracket_adjustment=None,
    )
    source = _FakeQuoteSource({"SCHW": _BID_ASK})

    (rewritten,) = await rewrite_enter_now_entries(
        (add,), quote_source=source, bps_through_touch=5.0
    )

    assert rewritten is add
    assert source.requested == []


def test_short_marketable_limit_crosses_to_or_below_the_bid() -> None:
    """A short (SELL) marketable limit sits at/below the bid so it crosses."""
    quote = TouchQuote(bid=price("85.10"), ask=price("85.14"))
    limit = marketable_limit_price(direction="short", quote=quote, bps_through_touch=5.0)
    # 85.10 * (1 - 0.0005) = 85.057... -> rounded DOWN to a penny = 85.05.
    assert limit == price("85.05")
    assert Decimal(limit) <= Decimal(quote.bid)


def test_long_marketable_limit_crosses_to_or_above_the_ask() -> None:
    """A long (BUY) marketable limit sits at/above the ask so it crosses."""
    quote = TouchQuote(bid=price("85.10"), ask=price("85.14"))
    limit = marketable_limit_price(direction="long", quote=quote, bps_through_touch=5.0)
    # 85.14 * (1 + 0.0005) = 85.182... -> rounded UP to a penny = 85.19.
    assert limit == price("85.19")
    assert Decimal(limit) >= Decimal(quote.ask)


def test_subpenny_tick_below_one_dollar() -> None:
    """Below $1.00 the limit rounds to the sub-penny ($0.0001) increment."""
    quote = TouchQuote(bid=price("0.5000"), ask=price("0.5020"))
    short_limit = marketable_limit_price(direction="short", quote=quote, bps_through_touch=10.0)
    # 0.5000 * (1 - 0.0010) = 0.4995 -> sub-penny tick, already exact.
    assert short_limit == price("0.4995")
    long_limit = marketable_limit_price(direction="long", quote=quote, bps_through_touch=10.0)
    # 0.5020 * (1 + 0.0010) = 0.50250... -> rounded UP to sub-penny = 0.5026.
    assert long_limit == price("0.5026")
