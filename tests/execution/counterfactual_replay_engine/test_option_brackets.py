"""Tests for option bracket simulation (ALP-562 §3, design Step 3 — option exit).

The bracket walk reuses story 05a's underlying-bar walker to find the trigger
(target / price-stop / time-stop), then translates the underlying trigger level
into the option's BS-derived premium at the exit-timestamp IV snapshot. Carries
``same_bar_ambiguity`` (from the underlying walk) and ``exit_iv_lag_minutes``.

Covers target / price-stop / time-stop exits, same-bar ambiguity propagation,
exit IV-lag population, and the exit-IV-miss sentinel (driver maps to
``DATA_MISSING``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from alphamind._kernel.money import money
from alphamind.execution.counterfactual_replay_engine.iv_lookup import (
    IVSnapshotLookupResult,
    resolve_contract_ticker,
)
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    OptionEntryResult,
    price_option_at_underlying_bar,
    simulate_option_brackets,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar
from alphamind.state.tables.counterfactual_replays import ExitLeg

_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_EXPIRATION = date(2026, 2, 20)
_RFR = 0.045


class _FakeIvRepo:
    def __init__(self, *, iv: float = 0.30, lag_minutes: float = 5.0, miss: bool = False) -> None:
        self._iv = iv
        self._lag_minutes = lag_minutes
        self._miss = miss

    def has_snapshot_at_or_before(self, contract_ticker: str, when: datetime) -> bool:
        return not self._miss

    def resolve_contract_ticker(
        self, *, underlying: str, strike: Decimal, expiration: date, contract_type: Any
    ) -> str:
        return resolve_contract_ticker(
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            contract_type=contract_type,
        )

    def lookup_iv(
        self, *, contract_ticker: str, target_ts: datetime
    ) -> IVSnapshotLookupResult | None:
        if self._miss:
            return None
        return IVSnapshotLookupResult(
            snapshot_ts=target_ts - timedelta(minutes=self._lag_minutes),
            implied_volatility=self._iv,
            underlying_price_at_snapshot=None,
            lag_minutes=self._lag_minutes,
        )


def _bar(*, offset: int, open_: float, high: float, low: float, close: float) -> OhlcvBar:
    start = _TS + timedelta(minutes=offset)
    return OhlcvBar(
        period_start=start,
        period_end=start + timedelta(minutes=15),
        open=open_,
        high=high,
        low=low,
        close=close,
        adj_volume=100_000,
    )


def _option_proposal(
    *,
    direction: str = "long",
    contract_type: str = "call",
    strike: str = "150.00",
    target_price: str = "170.00",
    price_stop: str = "140.00",
    time_stop_offset_minutes: int | None = None,
) -> Any:
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
            "instrument": {
                "asset_type": "option",
                "underlying": "AAPL",
                "strike": strike,
                "expiration": _EXPIRATION.isoformat(),
                "contract_type": contract_type,
                "direction": direction,
            },
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": {"type": "market"},
            "position_size": {
                "quantity": 5.0,
                "dollar_value": "2500.00",
                "pct_of_portfolio": 0.05,
                "premium_at_risk": "2500.00",
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


def _entry() -> OptionEntryResult:
    return OptionEntryResult(
        entered=True,
        entry_price=money(Decimal("3.00")),
        entry_timestamp=_TS + timedelta(minutes=15),
        entry_iv_lag_minutes=5.0,
    )


def test_target_hit_prices_option_at_target_underlying() -> None:
    proposal = _option_proposal(direction="long", contract_type="call", target_price="170.00")
    entry = _entry()
    bars = (
        _bar(offset=0, open_=150.0, high=151.0, low=149.0, close=150.5),
        _bar(offset=15, open_=151.0, high=152.0, low=150.0, close=151.5),
        # high 171 >= 170 target.
        _bar(offset=30, open_=152.0, high=171.0, low=151.0, close=170.5),
    )

    result = simulate_option_brackets(
        proposal, entry, bars, iv_repo=_FakeIvRepo(iv=0.30, lag_minutes=4.0), risk_free_rate=_RFR
    )

    assert result.exit_leg is ExitLeg.TARGET_HIT
    assert result.exit_underlying_price == 170.0
    assert result.exit_timestamp == bars[2].period_start
    assert result.exit_iv_lag_minutes == 4.0
    assert result.same_bar_ambiguity is False
    expected = price_option_at_underlying_bar(
        underlying_open=170.0,
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=bars[2].period_start,
        implied_volatility=0.30,
        risk_free_rate=_RFR,
    )
    assert result.exit_price == expected


def test_price_stop_hit_prices_option_at_stop_underlying() -> None:
    proposal = _option_proposal(direction="long", contract_type="call", price_stop="140.00")
    entry = _entry()
    bars = (
        _bar(offset=0, open_=150.0, high=151.0, low=149.0, close=150.5),
        # low 139 <= 140 stop; high 151 never reaches the 170 target.
        _bar(offset=15, open_=149.0, high=151.0, low=139.0, close=141.0),
    )

    result = simulate_option_brackets(
        proposal, entry, bars, iv_repo=_FakeIvRepo(iv=0.30), risk_free_rate=_RFR
    )

    assert result.exit_leg is ExitLeg.STOP_HIT
    assert result.exit_underlying_price == 140.0
    expected = price_option_at_underlying_bar(
        underlying_open=140.0,
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=bars[1].period_start,
        implied_volatility=0.30,
        risk_free_rate=_RFR,
    )
    assert result.exit_price == expected


def test_time_stop_prices_option_at_bar_open() -> None:
    proposal = _option_proposal(
        direction="long",
        contract_type="call",
        target_price="300.00",
        price_stop="100.00",
        time_stop_offset_minutes=45,
    )
    entry = _entry()
    bars = (
        _bar(offset=0, open_=150.0, high=151.0, low=149.0, close=150.5),
        _bar(offset=15, open_=151.0, high=152.0, low=150.0, close=151.5),
        _bar(offset=30, open_=152.0, high=153.0, low=151.0, close=152.5),
        # period_start 45 >= time-stop deadline 45 → fires here; underlying = open.
        _bar(offset=45, open_=152.5, high=154.0, low=151.0, close=153.0),
    )

    result = simulate_option_brackets(
        proposal, entry, bars, iv_repo=_FakeIvRepo(iv=0.30), risk_free_rate=_RFR
    )

    assert result.exit_leg is ExitLeg.TIME_STOP_FIRED
    assert result.exit_underlying_price == 152.5
    assert result.exit_timestamp == bars[3].period_start
    expected = price_option_at_underlying_bar(
        underlying_open=152.5,
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=bars[3].period_start,
        implied_volatility=0.30,
        risk_free_rate=_RFR,
    )
    assert result.exit_price == expected


def test_same_bar_ambiguity_propagated_from_underlying_walk() -> None:
    proposal = _option_proposal(
        direction="long", contract_type="call", target_price="170.00", price_stop="140.00"
    )
    entry = _entry()
    bars = (
        _bar(offset=0, open_=150.0, high=151.0, low=149.0, close=150.5),
        # high 171 >= 170 target AND low 139 <= 140 stop → stop fills first, flagged.
        _bar(offset=15, open_=151.0, high=171.0, low=139.0, close=155.0),
    )

    result = simulate_option_brackets(
        proposal, entry, bars, iv_repo=_FakeIvRepo(iv=0.30), risk_free_rate=_RFR
    )

    assert result.exit_leg is ExitLeg.STOP_HIT
    assert result.same_bar_ambiguity is True
    assert result.exit_underlying_price == 140.0


def test_exit_iv_miss_returns_sentinel() -> None:
    proposal = _option_proposal(direction="long", contract_type="call", target_price="170.00")
    entry = _entry()
    bars = (
        _bar(offset=0, open_=150.0, high=151.0, low=149.0, close=150.5),
        _bar(offset=15, open_=151.0, high=171.0, low=150.0, close=170.5),
    )

    result = simulate_option_brackets(
        proposal, entry, bars, iv_repo=_FakeIvRepo(miss=True), risk_free_rate=_RFR
    )

    # Sentinel: the underlying trigger is identified, but the exit option price
    # cannot be derived without IV. The driver (story 08) maps this to
    # DATA_MISSING; the simulator does not crash or fabricate an IV.
    assert result.exit_price is None
    assert result.exit_iv_lag_minutes is None
