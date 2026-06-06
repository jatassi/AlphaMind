"""Integration test for the equity replay driver (ALP-559, design Steps 2-4).

Exercises ``replay_equity_proposal`` end-to-end over a representative
happy-path proposal: a limit entry touched on a mid-window bar, a target hit on
a later bar, with real paper-harness drag. Verifies the driver composes the
entry, bracket, and P/L primitives into one :class:`EquityReplayResult`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alphamind._kernel.money import Money, price, signed_money
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.equity_replay import (
    replay_equity_proposal,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.positions import InstrumentType

_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_ADV = 5_000_000.0
_RVOL = 0.30

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


def _proposal() -> Any:
    from alphamind.decision.analyst.models import Recommendation

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-1",
            "instrument": {"asset_type": "equity", "ticker": "AAPL", "direction": "long"},
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


def test_happy_path_entry_on_bar2_target_on_bar5() -> None:
    bars = (
        _bar(offset=0, open_=185.0, high=186.0, low=184.0, close=185.5),
        # Bar 2 (index 1): low 179 touches the 180 limit → entry.
        _bar(offset=15, open_=184.0, high=185.0, low=179.0, close=181.0),
        _bar(offset=30, open_=181.0, high=183.0, low=180.5, close=182.0),
        _bar(offset=45, open_=182.0, high=185.0, low=181.0, close=184.0),
        # Bar 5 (index 4): high 201 hits the 200 target → exit.
        _bar(offset=60, open_=184.0, high=201.0, low=183.0, close=200.5),
    )

    result = replay_equity_proposal(
        _proposal(),
        bars,
        paper_harness_config=_CONFIG,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
    )

    assert result.entered is True
    assert result.entry_price == price(Decimal("180.00"))
    assert result.entry_timestamp == bars[1].period_start
    assert result.exit_leg is ExitLeg.TARGET_HIT
    assert result.exit_price == price(Decimal("200.00"))
    assert result.exit_timestamp == bars[4].period_start
    assert result.same_bar_ambiguity is False

    # P/L = (target - limit) * qty - entry/exit slippage - fees.
    entry_est = compute_live_execution_estimate(
        fill_price=price(Decimal("180.00")),
        fill_quantity=10.0,
        instrument_type=InstrumentType.EQUITY,
        side="buy",
        order_type=OrderType.limit,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
        config=_CONFIG,
    )
    exit_est = compute_live_execution_estimate(
        fill_price=price(Decimal("200.00")),
        fill_quantity=10.0,
        instrument_type=InstrumentType.EQUITY,
        side="sell",
        order_type=OrderType.limit,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
        config=_CONFIG,
    )
    assert entry_est is not None
    assert exit_est is not None
    entry_slip = Money(entry_est.estimated_spread_usd + entry_est.estimated_impact_usd)
    exit_slip = Money(exit_est.estimated_spread_usd + exit_est.estimated_impact_usd)
    gross = (Decimal("200.00") - Decimal("180.00")) * Decimal("10.0")
    expected_pl = signed_money(
        gross
        - entry_slip
        - entry_est.estimated_regulatory_fees_usd
        - exit_slip
        - exit_est.estimated_regulatory_fees_usd
    )
    assert result.realized_pl == expected_pl


def test_entry_unfilled_yields_unentered_result() -> None:
    # Limit far below the bar range → never touched.
    proposal = _proposal()
    bars = (
        _bar(offset=0, open_=185.0, high=186.0, low=184.0, close=185.5),
        _bar(offset=15, open_=185.0, high=186.0, low=184.5, close=185.0),
    )

    result = replay_equity_proposal(
        proposal,
        bars,
        paper_harness_config=_CONFIG,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
    )

    assert result.entered is False
    assert result.entry_price is None
    assert result.exit_leg is ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED
    assert result.exit_price is None
    assert result.realized_pl == signed_money(Decimal(0))
    assert result.exit_slippage is None
    assert result.exit_fees is None
