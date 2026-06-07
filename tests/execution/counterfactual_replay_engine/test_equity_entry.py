"""Tests for equity entry simulation (ALP-559, design Step 2).

Covers the four ``EntryOrder.type`` branches (limit long + short, market,
stop_limit long + short) and the entry-window-expired-unfilled case.

Bars are plain :class:`OhlcvBar` values, not mocks — the simulator is pure
compute over a bar sequence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alphamind._kernel.money import price
from alphamind.execution.counterfactual_replay_engine.equity_replay import (
    simulate_equity_entry,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar

_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)


def _bar(
    *,
    minute_offset: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> OhlcvBar:
    start = _TS + timedelta(minutes=minute_offset)
    return OhlcvBar(
        period_start=start,
        period_end=start + timedelta(minutes=15),
        open=open_,
        high=high,
        low=low,
        close=close,
        adj_volume=100_000,
    )


def _equity_recommendation(
    *,
    entry_order: dict[str, Any],
    direction: str = "long",
) -> Any:
    """Build a minimal equity Recommendation with the given entry order."""
    from alphamind.decision.analyst.models import Recommendation

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-1",
            "instrument": {"asset_type": "equity", "ticker": "AAPL", "direction": direction},
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": entry_order,
            "position_size": {
                "quantity": 10.0,
                "dollar_value": "1000.00",
                "pct_of_portfolio": 0.05,
            },
            "target": {
                "target_type": "absolute_price",
                "price": "200.00",
                "dollar_pl_target": "500.00",
            },
            "invalidation_legs": [
                {
                    "leg_id": "INV-1",
                    "type": "price",
                    "is_hard": True,
                    "condition": {
                        "underlying_trigger": "AAPL",
                        "comparator": "<=",
                        "trigger_price": "170.00",
                    },
                    "order_parameters": {"order_type": "market"},
                }
            ],
            "time_expectation_hours": 8.0,
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [],
                "checked_at": "2026-01-10T14:00:00+00:00",
            },
            "thesis_narrative": "Test thesis.",
            "target_rationale": "Test target.",
            "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support broken."}],
            "position_size_rationale": "Standard allocation.",
            "counterarguments_acknowledged": "Acknowledged.",
        }
    )


def test_limit_long_fills_at_limit_on_first_touch() -> None:
    proposal = _equity_recommendation(
        entry_order={"type": "limit", "limit_price": "175.00"},
        direction="long",
    )
    bars = (
        _bar(minute_offset=0, open_=178.0, high=179.0, low=176.0, close=177.0),
        _bar(minute_offset=15, open_=177.0, high=177.5, low=174.0, close=175.5),
        _bar(minute_offset=30, open_=175.0, high=176.0, low=173.0, close=174.0),
    )

    result = simulate_equity_entry(proposal, bars)

    assert result.entered is True
    assert result.entry_price == price(Decimal("175.00"))
    # First bar whose low <= 175 is the second bar (low 174.0).
    assert result.entry_timestamp == bars[1].period_start


def test_limit_short_fills_at_limit_on_first_touch() -> None:
    proposal = _equity_recommendation(
        entry_order={"type": "limit", "limit_price": "180.00"},
        direction="short",
    )
    bars = (
        _bar(minute_offset=0, open_=176.0, high=178.0, low=175.0, close=177.0),
        _bar(minute_offset=15, open_=178.0, high=181.0, low=177.0, close=180.5),
        _bar(minute_offset=30, open_=180.0, high=182.0, low=179.0, close=181.0),
    )

    result = simulate_equity_entry(proposal, bars)

    assert result.entered is True
    assert result.entry_price == price(Decimal("180.00"))
    # First bar whose high >= 180 is the second bar (high 181.0).
    assert result.entry_timestamp == bars[1].period_start


def test_market_fills_at_next_bar_open() -> None:
    proposal = _equity_recommendation(entry_order={"type": "market"}, direction="long")
    bars = (
        _bar(minute_offset=0, open_=178.0, high=179.0, low=176.0, close=177.0),
        _bar(minute_offset=15, open_=177.0, high=177.5, low=174.0, close=175.5),
    )

    result = simulate_equity_entry(proposal, bars)

    assert result.entered is True
    # Market fills at the open of the bar following the proposal (bars[1]).
    assert result.entry_price == price(Decimal("177.0"))
    assert result.entry_timestamp == bars[1].period_start


def test_stop_limit_long_arms_on_stop_then_fills_on_limit() -> None:
    proposal = _equity_recommendation(
        entry_order={"type": "stop_limit", "stop_price": "180.00", "limit_price": "178.00"},
        direction="long",
    )
    bars = (
        # Below stop — not armed yet.
        _bar(minute_offset=0, open_=176.0, high=179.0, low=175.0, close=177.0),
        # High >= 180 arms the stop; its low (179) never reaches the 178 limit.
        _bar(minute_offset=15, open_=179.0, high=181.0, low=179.0, close=180.5),
        # Subsequent bar low <= 178 gates the fill.
        _bar(minute_offset=30, open_=180.0, high=180.5, low=177.0, close=178.0),
    )

    result = simulate_equity_entry(proposal, bars)

    assert result.entered is True
    assert result.entry_price == price(Decimal("178.00"))
    assert result.entry_timestamp == bars[2].period_start


def test_stop_limit_short_arms_on_stop_then_fills_on_limit() -> None:
    proposal = _equity_recommendation(
        entry_order={"type": "stop_limit", "stop_price": "170.00", "limit_price": "172.00"},
        direction="short",
    )
    bars = (
        # Above stop — not armed yet.
        _bar(minute_offset=0, open_=174.0, high=175.0, low=171.0, close=173.0),
        # Low <= 170 arms the stop; its high (171) never reaches the 172 limit.
        _bar(minute_offset=15, open_=171.0, high=171.0, low=169.0, close=170.0),
        # Subsequent bar high >= 172 gates the fill.
        _bar(minute_offset=30, open_=170.0, high=173.0, low=169.5, close=172.0),
    )

    result = simulate_equity_entry(proposal, bars)

    assert result.entered is True
    assert result.entry_price == price(Decimal("172.00"))
    assert result.entry_timestamp == bars[2].period_start


def test_no_fill_across_window_records_unentered() -> None:
    proposal = _equity_recommendation(
        entry_order={"type": "limit", "limit_price": "150.00"},
        direction="long",
    )
    bars = (
        _bar(minute_offset=0, open_=178.0, high=179.0, low=176.0, close=177.0),
        _bar(minute_offset=15, open_=177.0, high=177.5, low=174.0, close=175.5),
    )

    result = simulate_equity_entry(proposal, bars)

    assert result.entered is False
    assert result.entry_price is None
    assert result.entry_timestamp is None
