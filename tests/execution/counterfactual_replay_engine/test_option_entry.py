"""Tests for option entry simulation (ALP-562 §2, design Step 2 — option branch).

Entry is a market-style fill at the bar *following* the proposal bar, regardless
of ``entry_order.type``. The entry premium is BS-derived from the next bar's
open, the per-contract IV snapshot at the entry timestamp, and the bar's
time-to-expiration. ``entry_iv_lag_minutes`` carries the snapshot lag for the
confidence classifier (story 05b).

Covers long-call / long-put / short-call / short-put. The IV repository is a
small in-memory fake implementing only ``resolve_contract_ticker`` + ``lookup_iv``
(the two methods this path exercises); ``bs_price`` is exercised for real.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from alphamind.execution.counterfactual_replay_engine.iv_lookup import (
    IVSnapshotLookupResult,
    resolve_contract_ticker,
)
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    price_option_at_underlying_bar,
    simulate_option_entry,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar

_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_EXPIRATION = date(2026, 2, 20)
_RFR = 0.045


class _FakeIvRepo:
    """In-memory IV repo: one IV at a fixed lag for any contract / timestamp."""

    def __init__(self, *, iv: float = 0.30, lag_minutes: float = 7.0) -> None:
        self._iv = iv
        self._lag_minutes = lag_minutes
        self.calls: list[tuple[str, datetime]] = []

    def has_snapshot_at_or_before(self, contract_ticker: str, when: datetime) -> bool:
        return True

    def resolve_contract_ticker(
        self,
        *,
        underlying: str,
        strike: Decimal,
        expiration: date,
        contract_type: Any,
    ) -> str:
        return resolve_contract_ticker(
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            contract_type=contract_type,
        )

    def lookup_iv(self, *, contract_ticker: str, target_ts: datetime) -> IVSnapshotLookupResult:
        self.calls.append((contract_ticker, target_ts))
        return IVSnapshotLookupResult(
            snapshot_ts=target_ts - timedelta(minutes=self._lag_minutes),
            implied_volatility=self._iv,
            underlying_price_at_snapshot=None,
            lag_minutes=self._lag_minutes,
        )


def _bar(*, offset: int, open_: float) -> OhlcvBar:
    start = _TS + timedelta(minutes=offset)
    return OhlcvBar(
        period_start=start,
        period_end=start + timedelta(minutes=15),
        open=open_,
        high=open_ + 1.0,
        low=open_ - 1.0,
        close=open_,
        adj_volume=100_000,
    )


def _option_proposal(
    *,
    direction: str,
    contract_type: str,
    strike: str = "150.00",
    entry_type: str = "market",
    limit_price: str | None = None,
) -> Any:
    from alphamind.decision.analyst.models import Recommendation

    entry_order: dict[str, Any] = {"type": entry_type}
    if limit_price is not None:
        entry_order["limit_price"] = limit_price

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
            "entry_order": entry_order,
            "position_size": {
                "quantity": 5.0,
                "dollar_value": "2500.00",
                "pct_of_portfolio": 0.05,
                "premium_at_risk": "2500.00",
            },
            "target": {
                "target_type": "absolute_price",
                "price": "170.00",
                "dollar_pl_target": "500.00",
            },
            "invalidation_legs": [
                {
                    "leg_id": "INV-1",
                    "type": "price",
                    "is_hard": True,
                    "condition": {
                        "underlying_trigger": "AAPL",
                        "comparator": "<=" if direction == "long" else ">=",
                        "trigger_price": "140.00",
                    },
                    "order_parameters": {"order_type": "stop"},
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


def _bars() -> tuple[OhlcvBar, ...]:
    # bars[0] contains the proposal; bars[1] is the fill bar.
    return (_bar(offset=0, open_=150.0), _bar(offset=15, open_=151.0))


def test_long_call_entry_fills_at_next_bar_open() -> None:
    proposal = _option_proposal(direction="long", contract_type="call")
    repo = _FakeIvRepo(iv=0.30, lag_minutes=7.0)
    bars = _bars()

    result = simulate_option_entry(proposal, bars, iv_repo=repo, risk_free_rate=_RFR)

    assert result.entered is True
    assert result.entry_timestamp == bars[1].period_start
    assert result.entry_iv_lag_minutes == 7.0
    expected = price_option_at_underlying_bar(
        underlying_open=151.0,
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=bars[1].period_start,
        implied_volatility=0.30,
        risk_free_rate=_RFR,
    )
    assert result.entry_price == expected


def test_long_put_entry_prices_a_put() -> None:
    proposal = _option_proposal(direction="long", contract_type="put")
    repo = _FakeIvRepo(iv=0.35, lag_minutes=3.0)
    bars = _bars()

    result = simulate_option_entry(proposal, bars, iv_repo=repo, risk_free_rate=_RFR)

    expected = price_option_at_underlying_bar(
        underlying_open=151.0,
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="put",
        bar_timestamp=bars[1].period_start,
        implied_volatility=0.35,
        risk_free_rate=_RFR,
    )
    assert result.entry_price == expected
    assert result.entry_iv_lag_minutes == 3.0


def test_short_call_entry_fires_like_long() -> None:
    # Direction does not change the BS premium at entry; it is recorded for the
    # P/L sign. Confirms short entries still fire at the next bar.
    proposal = _option_proposal(direction="short", contract_type="call")
    repo = _FakeIvRepo()
    result = simulate_option_entry(proposal, _bars(), iv_repo=repo, risk_free_rate=_RFR)
    assert result.entered is True


def test_short_put_entry_fires_like_long() -> None:
    proposal = _option_proposal(direction="short", contract_type="put")
    repo = _FakeIvRepo()
    result = simulate_option_entry(proposal, _bars(), iv_repo=repo, risk_free_rate=_RFR)
    assert result.entered is True


def test_limit_entry_type_still_fills_at_next_bar() -> None:
    # A limit entry_order does NOT gate option fill timing (v2 simplification):
    # entry still fires at the proposal-following bar. limit_price is recorded
    # upstream but not consulted here.
    proposal = _option_proposal(
        direction="long", contract_type="call", entry_type="limit", limit_price="3.50"
    )
    repo = _FakeIvRepo()
    bars = _bars()

    result = simulate_option_entry(proposal, bars, iv_repo=repo, risk_free_rate=_RFR)

    assert result.entered is True
    assert result.entry_timestamp == bars[1].period_start


def test_lookup_uses_resolved_contract_ticker_at_entry_timestamp() -> None:
    proposal = _option_proposal(direction="long", contract_type="call", strike="150.00")
    repo = _FakeIvRepo()
    bars = _bars()

    simulate_option_entry(proposal, bars, iv_repo=repo, risk_free_rate=_RFR)

    expected_ticker = resolve_contract_ticker(
        underlying="AAPL",
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="call",
    )
    assert repo.calls == [(expected_ticker, bars[1].period_start)]
