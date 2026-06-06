"""Tests for equity P/L composition (ALP-559, design Step 4).

Covers long-profit / long-loss / short-profit / short-loss and the
missing-ADV → zero-slippage-and-fees fallback.

The paper harness's :func:`compute_live_execution_estimate` is pure compute, so
it is exercised for real (a real :class:`PaperHarness` config plus ADV / vol),
not mocked. Tests assert the Step-4 composition relationship against the
harness's own output rather than hard-coded drag numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alphamind._kernel.money import Money, price, signed_money
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.equity_replay import (
    EquityBracketResult,
    EquityEntryResult,
    compute_equity_pl,
)
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


def _equity_recommendation(*, direction: str = "long", quantity: float = 10.0) -> Any:
    from alphamind.decision.analyst.models import Recommendation

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-1",
            "instrument": {"asset_type": "equity", "ticker": "AAPL", "direction": direction},
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": {"type": "limit", "limit_price": "180.00"},
            "position_size": {
                "quantity": quantity,
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
                        "comparator": "<=" if direction == "long" else ">=",
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


def _entry(entry_price: str) -> EquityEntryResult:
    return EquityEntryResult(
        entered=True,
        entry_price=price(Decimal(entry_price)),
        entry_timestamp=_TS,
    )


def _exit(exit_price: str, leg: ExitLeg) -> EquityBracketResult:
    return EquityBracketResult(
        exit_leg=leg,
        exit_price=price(Decimal(exit_price)),
        exit_timestamp=_TS + timedelta(hours=2),
        same_bar_ambiguity=False,
    )


def _drag(fill_price: str, side: str, order_type: OrderType) -> tuple[Money, Money]:
    """Return (slippage, fees) the harness would attribute for a fill."""
    est = compute_live_execution_estimate(
        fill_price=price(Decimal(fill_price)),
        fill_quantity=10.0,
        instrument_type=InstrumentType.EQUITY,
        side=side,  # type: ignore[arg-type]
        order_type=order_type,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
        config=_CONFIG,
    )
    assert est is not None
    slippage = Money(est.estimated_spread_usd + est.estimated_impact_usd)
    return slippage, est.estimated_regulatory_fees_usd


def test_long_loss_composes_step4_formula_and_is_negative() -> None:
    proposal = _equity_recommendation(direction="long")
    entry = _entry("180.00")
    brackets = _exit("170.00", ExitLeg.STOP_HIT)

    result = compute_equity_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
    )

    # Long: entry buy (limit), exit sell (stop). Pins both the Step-4 formula
    # and the entry/exit order-type → drag mapping; the long-profit path is
    # covered end-to-end by test_equity_replay.
    entry_slip, entry_fees = _drag("180.00", "buy", OrderType.limit)
    exit_slip, exit_fees = _drag("170.00", "sell", OrderType.stop)
    gross = (Decimal("170.00") - Decimal("180.00")) * Decimal("10.0") * Decimal(1)
    expected = signed_money(gross - entry_slip - entry_fees - exit_slip - exit_fees)

    assert result.realized_pl == expected
    assert result.realized_pl < 0
    assert result.entry_slippage == entry_slip
    assert result.entry_fees == entry_fees
    assert result.exit_slippage == exit_slip
    assert result.exit_fees == exit_fees


def test_short_profit_is_positive() -> None:
    # Short profits when exit < entry: direction_sign = -1.
    proposal = _equity_recommendation(direction="short")
    entry = _entry("180.00")
    brackets = _exit("170.00", ExitLeg.TARGET_HIT)

    result = compute_equity_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
    )

    # Short: entry sell (limit), exit buy (target → limit).
    entry_slip, entry_fees = _drag("180.00", "sell", OrderType.limit)
    exit_slip, exit_fees = _drag("170.00", "buy", OrderType.limit)
    gross = (Decimal("170.00") - Decimal("180.00")) * Decimal("10.0") * Decimal(-1)
    expected = signed_money(gross - entry_slip - entry_fees - exit_slip - exit_fees)

    assert result.realized_pl == expected
    assert result.realized_pl > 0


def test_short_loss_is_negative() -> None:
    proposal = _equity_recommendation(direction="short")
    entry = _entry("180.00")
    brackets = _exit("190.00", ExitLeg.STOP_HIT)

    result = compute_equity_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_shares=_ADV,
        realized_volatility=_RVOL,
    )

    entry_slip, entry_fees = _drag("180.00", "sell", OrderType.limit)
    exit_slip, exit_fees = _drag("190.00", "buy", OrderType.stop)
    gross = (Decimal("190.00") - Decimal("180.00")) * Decimal("10.0") * Decimal(-1)
    expected = signed_money(gross - entry_slip - entry_fees - exit_slip - exit_fees)

    assert result.realized_pl == expected
    assert result.realized_pl < 0


def test_missing_adv_records_zero_slippage_and_fees() -> None:
    proposal = _equity_recommendation(direction="long")
    entry = _entry("180.00")
    brackets = _exit("200.00", ExitLeg.TARGET_HIT)

    result = compute_equity_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_shares=None,
        realized_volatility=_RVOL,
    )

    # No estimate → slippage + fees zero on both sides; realized P/L is gross.
    gross = (Decimal("200.00") - Decimal("180.00")) * Decimal("10.0") * Decimal(1)
    assert result.realized_pl == signed_money(gross)
    assert result.entry_slippage == Money(Decimal(0))
    assert result.entry_fees == Money(Decimal(0))
    assert result.exit_slippage == Money(Decimal(0))
    assert result.exit_fees == Money(Decimal(0))
