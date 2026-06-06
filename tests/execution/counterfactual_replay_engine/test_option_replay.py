"""Integration test for the option replay driver (ALP-562 §5, design Steps 2-4).

Exercises ``replay_option_proposal`` end-to-end over a representative happy-path
long-call proposal: a market-style entry at the proposal-following bar, the
underlying hitting the target on a later bar, with real BS pricing and real
paper-harness drag. Also covers the exit-IV-miss sentinel that the engine
driver (story 08) maps to ``DATA_MISSING``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.iv_lookup import (
    IVSnapshotLookupResult,
    resolve_contract_ticker,
)
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    price_option_at_underlying_bar,
    replay_option_proposal,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar

_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_EXPIRATION = date(2026, 2, 20)
_RFR = 0.045
_ADV_CONTRACTS = 20_000.0
_RVOL = 0.30
_IV = 0.30

_CONFIG = PaperHarness(
    spread_buffer_pct=0.1,
    impact_coefficients={OrderType.market: 0.5, OrderType.limit: 0.3, OrderType.stop: 0.4},
    fee_schedule=FeeSchedule(
        cat_per_executed_share=0.0001,
        taf_per_share_sells=0.000166,
        sec_pct_of_notional_sells=0.0000278,
        orf_per_options_contract=0.02685,
        occ_per_options_contract=0.02,
    ),
)


class _FakeIvRepo:
    """In-memory IV repo. ``miss_after`` makes lookups past a timestamp return
    ``None`` so the exit-side miss can be isolated from the entry-side hit."""

    def __init__(self, *, miss_after: datetime | None = None) -> None:
        self._miss_after = miss_after

    def has_snapshot_at_or_before(self, contract_ticker: str, when: datetime) -> bool:
        return self._miss_after is None or when <= self._miss_after

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
        if self._miss_after is not None and target_ts > self._miss_after:
            return None
        return IVSnapshotLookupResult(
            snapshot_ts=target_ts - timedelta(minutes=6.0),
            implied_volatility=_IV,
            underlying_price_at_snapshot=None,
            lag_minutes=6.0,
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
        adj_volume=200_000,
    )


def _long_call() -> Any:
    from alphamind.decision.analyst.models import Recommendation

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-1",
            "instrument": {
                "asset_type": "option",
                "underlying": "AAPL",
                "strike": "150.00",
                "expiration": _EXPIRATION.isoformat(),
                "contract_type": "call",
                "direction": "long",
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
                        "comparator": "<=",
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


def _bars_target_on_bar4() -> tuple[OhlcvBar, ...]:
    return (
        _bar(offset=0, open_=150.0, high=151.0, low=149.0, close=150.5),
        # entry fill bar (index 1): open 151.
        _bar(offset=15, open_=151.0, high=152.0, low=150.0, close=151.5),
        _bar(offset=30, open_=152.0, high=160.0, low=151.0, close=159.0),
        # target bar (index 3): high 171 >= 170.
        _bar(offset=45, open_=159.0, high=171.0, low=158.0, close=170.5),
    )


def test_long_call_target_hit_yields_positive_pl() -> None:
    proposal = _long_call()
    bars = _bars_target_on_bar4()

    result = replay_option_proposal(
        proposal,
        bars,
        iv_repo=_FakeIvRepo(),
        paper_harness_config=_CONFIG,
        risk_free_rate=_RFR,
        adv_contracts=_ADV_CONTRACTS,
        realized_volatility=_RVOL,
    )

    assert result.entered is True
    assert result.entry_timestamp == bars[1].period_start
    assert result.exit_underlying_price == 170.0
    assert result.exit_leg is ExitLeg.TARGET_HIT
    assert result.exit_timestamp == bars[3].period_start
    assert result.same_bar_ambiguity is False
    assert result.entry_iv_lag_minutes == 6.0
    assert result.exit_iv_lag_minutes == 6.0

    # Entry premium derived from the next bar's open (151); exit premium from the
    # target underlying (170). The underlying rose, so the call gained value.
    entry_premium = price_option_at_underlying_bar(
        underlying_open=151.0,
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=bars[1].period_start,
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )
    exit_premium = price_option_at_underlying_bar(
        underlying_open=170.0,
        strike=Decimal("150.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=bars[3].period_start,
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )
    assert result.entry_price == entry_premium
    assert result.exit_price == exit_premium
    assert exit_premium > entry_premium
    assert result.realized_pl is not None
    assert result.realized_pl > 0


def test_exit_iv_miss_yields_data_missing_sentinel() -> None:
    # IV present at the entry bar but missing at the (later) exit bar: the
    # driver's exit-side sentinel branch fires. realized_pl=None → DATA_MISSING,
    # but the entry IV lag survives as a diagnostic.
    proposal = _long_call()
    bars = _bars_target_on_bar4()

    result = replay_option_proposal(
        proposal,
        bars,
        iv_repo=_FakeIvRepo(miss_after=bars[1].period_start),
        paper_harness_config=_CONFIG,
        risk_free_rate=_RFR,
        adv_contracts=_ADV_CONTRACTS,
        realized_volatility=_RVOL,
    )

    assert result.realized_pl is None
    assert result.exit_price is None
    assert result.entry_iv_lag_minutes == 6.0


def test_entry_iv_miss_yields_data_missing_sentinel() -> None:
    # IV missing at the entry bar: the driver gates before simulating, so even
    # the entry IV lag is None. No fabricated IV, no crash.
    proposal = _long_call()
    bars = _bars_target_on_bar4()

    result = replay_option_proposal(
        proposal,
        bars,
        iv_repo=_FakeIvRepo(miss_after=_TS),
        paper_harness_config=_CONFIG,
        risk_free_rate=_RFR,
        adv_contracts=_ADV_CONTRACTS,
        realized_volatility=_RVOL,
    )

    assert result.realized_pl is None
    assert result.entry_price is None
    assert result.entry_iv_lag_minutes is None
