"""Tests for equity bracket simulation (ALP-559, design Step 3).

Covers target hit / price-stop hit / time-stop fired in the documented order,
same-bar target-and-stop ambiguity (stop fills first, flagged), and the
entry-unfilled short-circuit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alphamind._kernel.money import price
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.equity_replay import (
    EquityEntryResult,
    simulate_equity_brackets,
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
    direction: str = "long",
    target_price: str = "200.00",
    price_stop: str = "170.00",
    time_stop_offset_minutes: int | None = None,
) -> Any:
    """Build an equity Recommendation with a price stop and optional time stop."""
    from alphamind.decision.analyst.models import Recommendation

    invalidation_legs: list[dict[str, Any]] = [
        {
            "leg_id": "INV-1",
            "type": "price",
            "is_hard": True,
            "condition": {
                "underlying_trigger": "AAPL",
                "comparator": "<=" if direction == "long" else ">=",
                "trigger_price": price_stop,
            },
            "order_parameters": {"order_type": "stop"},
        }
    ]
    if time_stop_offset_minutes is not None:
        deadline = _TS + timedelta(minutes=time_stop_offset_minutes)
        invalidation_legs.append(
            {
                "leg_id": "INV-2",
                "type": "time",
                "is_hard": True,
                "condition": {"deadline": deadline.isoformat()},
                "order_parameters": {"order_type": "market"},
            }
        )

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-1",
            "instrument": {"asset_type": "equity", "ticker": "AAPL", "direction": direction},
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": {"type": "limit", "limit_price": "180.00"},
            "position_size": {
                "quantity": 10.0,
                "dollar_value": "1800.00",
                "pct_of_portfolio": 0.05,
            },
            "target": {
                "target_type": "absolute_price",
                "price": target_price,
                "dollar_pl_target": "500.00",
            },
            "invalidation_legs": invalidation_legs,
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


def _entered_at(minute_offset: int) -> EquityEntryResult:
    return EquityEntryResult(
        entered=True,
        entry_price=price(Decimal("180.00")),
        entry_timestamp=_TS + timedelta(minutes=minute_offset),
    )


def test_target_hit_long_records_at_target_price() -> None:
    proposal = _equity_recommendation(direction="long", target_price="200.00")
    entry = _entered_at(0)
    bars = (
        _bar(minute_offset=0, open_=180.0, high=182.0, low=179.0, close=181.0),
        _bar(minute_offset=15, open_=181.0, high=201.0, low=180.0, close=200.5),
    )

    result = simulate_equity_brackets(proposal, entry, bars)

    assert result.exit_leg is ExitLeg.TARGET_HIT
    assert result.exit_price == price(Decimal("200.00"))
    assert result.exit_timestamp == bars[1].period_start
    assert result.same_bar_ambiguity is False


def test_price_stop_hit_long_records_at_stop_trigger() -> None:
    proposal = _equity_recommendation(direction="long", price_stop="170.00")
    entry = _entered_at(0)
    bars = (
        _bar(minute_offset=0, open_=180.0, high=182.0, low=179.0, close=181.0),
        # Low 168 crosses the 170 stop; high 181 never reaches the 200 target.
        _bar(minute_offset=15, open_=179.0, high=181.0, low=168.0, close=171.0),
    )

    result = simulate_equity_brackets(proposal, entry, bars)

    assert result.exit_leg is ExitLeg.STOP_HIT
    assert result.exit_price == price(Decimal("170.00"))
    assert result.exit_timestamp == bars[1].period_start
    assert result.same_bar_ambiguity is False


def test_price_stop_hit_short_records_at_stop_trigger() -> None:
    proposal = _equity_recommendation(direction="short", target_price="160.00", price_stop="190.00")
    entry = _entered_at(0)
    bars = (
        _bar(minute_offset=0, open_=180.0, high=182.0, low=179.0, close=181.0),
        # Short stop: high 191 >= 190 stop; low 179 never reaches the 160 target.
        _bar(minute_offset=15, open_=181.0, high=191.0, low=179.0, close=189.0),
    )

    result = simulate_equity_brackets(proposal, entry, bars)

    assert result.exit_leg is ExitLeg.STOP_HIT
    assert result.exit_price == price(Decimal("190.00"))
    assert result.exit_timestamp == bars[1].period_start


def test_time_stop_fired_records_at_bar_open() -> None:
    # Time stop 30 minutes out; price never crosses target or stop.
    proposal = _equity_recommendation(
        direction="long", price_stop="100.00", time_stop_offset_minutes=30
    )
    entry = _entered_at(0)
    bars = (
        _bar(minute_offset=0, open_=180.0, high=182.0, low=179.0, close=181.0),
        _bar(minute_offset=15, open_=181.0, high=183.0, low=180.0, close=182.0),
        # period_start (30 min) >= time-stop deadline (30 min) → fires here.
        _bar(minute_offset=30, open_=182.5, high=184.0, low=181.0, close=183.0),
    )

    result = simulate_equity_brackets(proposal, entry, bars)

    assert result.exit_leg is ExitLeg.TIME_STOP_FIRED
    assert result.exit_price == price(Decimal("182.5"))
    assert result.exit_timestamp == bars[2].period_start


def test_same_bar_target_and_stop_flags_ambiguity_as_stop() -> None:
    proposal = _equity_recommendation(direction="long", target_price="200.00", price_stop="170.00")
    entry = _entered_at(0)
    bars = (
        _bar(minute_offset=0, open_=180.0, high=182.0, low=179.0, close=181.0),
        # Same bar: high 201 >= 200 target AND low 168 <= 170 stop.
        _bar(minute_offset=15, open_=181.0, high=201.0, low=168.0, close=185.0),
    )

    result = simulate_equity_brackets(proposal, entry, bars)

    # Design: assume the stop fills first; flag the ambiguity.
    assert result.exit_leg is ExitLeg.STOP_HIT
    assert result.exit_price == price(Decimal("170.00"))
    assert result.same_bar_ambiguity is True


def test_entry_unfilled_short_circuits_to_window_expired() -> None:
    proposal = _equity_recommendation(direction="long")
    entry = EquityEntryResult(entered=False, entry_price=None, entry_timestamp=None)
    bars = (_bar(minute_offset=0, open_=180.0, high=182.0, low=179.0, close=181.0),)

    result = simulate_equity_brackets(proposal, entry, bars)

    assert result.exit_leg is ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED
    assert result.exit_price is None
    assert result.exit_timestamp is None
    assert result.same_bar_ambiguity is False


def test_window_exhausted_without_price_exit_resolves_as_time_stop_at_last_bar() -> None:
    # Only a hard price leg, no time leg; price never crosses target or stop, so
    # the bar walk exhausts at the thesis-horizon-bounded window end.
    proposal = _equity_recommendation(direction="long", target_price="300.00", price_stop="100.00")
    entry = _entered_at(0)
    bars = (
        _bar(minute_offset=0, open_=180.0, high=182.0, low=179.0, close=181.0),
        _bar(minute_offset=15, open_=181.0, high=183.0, low=180.0, close=182.0),
        _bar(minute_offset=30, open_=182.0, high=184.0, low=181.0, close=183.0),
    )

    result = simulate_equity_brackets(proposal, entry, bars)

    assert result.exit_leg is ExitLeg.TIME_STOP_FIRED
    assert result.exit_price == price(Decimal("182.0"))
    assert result.exit_timestamp == bars[2].period_start
    assert result.same_bar_ambiguity is False
