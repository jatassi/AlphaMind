"""Tests for option P/L composition (ALP-562 §4, design Step 4 — options).

``realized_pl = (exit_price - entry_price) * quantity * 100 * direction_sign
- entry_slippage - entry_fees - exit_slippage - exit_fees``, where ``100`` is
the standard US options contract multiplier. Slippage / fees come from the
paper harness's :func:`compute_live_execution_estimate` with
``instrument_type=OPTIONS``; a ``None`` harness return records that side's
slippage and fees as zero.

Covers long-profit / long-loss / short-profit / short-loss and the missing-ADV
→ zero-slippage fallback. The harness is pure compute and exercised for real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from alphamind._kernel.money import Money, price, signed_money
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    OptionBracketResult,
    OptionEntryResult,
    compute_option_pl,
)
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.positions import InstrumentType

_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_ADV_CONTRACTS = 20_000.0
_RVOL = 0.30
_QTY = 5.0
_MULTIPLIER = Decimal(100)

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


def _option_proposal(*, direction: str) -> Any:
    from alphamind.decision.analyst.models import Recommendation

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-1",
            "instrument": {
                "asset_type": "option",
                "underlying": "AAPL",
                "strike": "150.00",
                "expiration": "2026-02-20",
                "contract_type": "call",
                "direction": direction,
            },
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": {"type": "market"},
            "position_size": {
                "quantity": _QTY,
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


def _entry(premium: str) -> OptionEntryResult:
    return OptionEntryResult(
        entered=True,
        entry_price=price(Decimal(premium)),
        entry_timestamp=_TS,
        entry_iv_lag_minutes=5.0,
    )


def _brackets(premium: str, leg: ExitLeg) -> OptionBracketResult:
    return OptionBracketResult(
        exit_leg=leg,
        exit_underlying_price=170.0,
        exit_price=price(Decimal(premium)),
        exit_timestamp=_TS + timedelta(hours=2),
        same_bar_ambiguity=False,
        exit_iv_lag_minutes=4.0,
    )


def _drag(
    premium: str, side: Literal["buy", "sell"], order_type: OrderType, *, adv: float | None
) -> tuple[Money, Money]:
    est = compute_live_execution_estimate(
        fill_price=price(Decimal(premium)),
        fill_quantity=_QTY,
        instrument_type=InstrumentType.OPTIONS,
        side=side,
        order_type=order_type,
        adv_shares=adv,
        realized_volatility=_RVOL,
        config=_CONFIG,
    )
    if est is None:
        return Money(Decimal(0)), Money(Decimal(0))
    slippage = Money(est.estimated_spread_usd + est.estimated_impact_usd)
    return slippage, est.estimated_regulatory_fees_usd


def test_long_profit_applies_multiplier_100() -> None:
    proposal = _option_proposal(direction="long")
    entry = _entry("3.00")
    brackets = _brackets("8.00", ExitLeg.TARGET_HIT)

    result = compute_option_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_contracts=_ADV_CONTRACTS,
        realized_volatility=_RVOL,
    )

    # Long: entry buy (market), exit sell (target → limit).
    entry_slip, entry_fees = _drag("3.00", "buy", OrderType.market, adv=_ADV_CONTRACTS)
    exit_slip, exit_fees = _drag("8.00", "sell", OrderType.limit, adv=_ADV_CONTRACTS)
    gross = (Decimal("8.00") - Decimal("3.00")) * Decimal(str(_QTY)) * _MULTIPLIER * Decimal(1)
    expected = signed_money(gross - entry_slip - entry_fees - exit_slip - exit_fees)

    assert result.realized_pl == expected
    assert result.realized_pl > 0
    assert result.entry_slippage == entry_slip
    assert result.entry_fees == entry_fees
    assert result.exit_slippage == exit_slip
    assert result.exit_fees == exit_fees


def test_long_loss_is_negative() -> None:
    proposal = _option_proposal(direction="long")
    entry = _entry("8.00")
    brackets = _brackets("2.00", ExitLeg.STOP_HIT)

    result = compute_option_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_contracts=_ADV_CONTRACTS,
        realized_volatility=_RVOL,
    )

    entry_slip, entry_fees = _drag("8.00", "buy", OrderType.market, adv=_ADV_CONTRACTS)
    exit_slip, exit_fees = _drag("2.00", "sell", OrderType.stop, adv=_ADV_CONTRACTS)
    gross = (Decimal("2.00") - Decimal("8.00")) * Decimal(str(_QTY)) * _MULTIPLIER * Decimal(1)
    expected = signed_money(gross - entry_slip - entry_fees - exit_slip - exit_fees)

    assert result.realized_pl == expected
    assert result.realized_pl < 0


def test_short_profit_is_positive() -> None:
    # Short profits when exit premium < entry premium: direction_sign = -1.
    proposal = _option_proposal(direction="short")
    entry = _entry("8.00")
    brackets = _brackets("3.00", ExitLeg.TARGET_HIT)

    result = compute_option_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_contracts=_ADV_CONTRACTS,
        realized_volatility=_RVOL,
    )

    # Short: entry sell (market), exit buy (target → limit).
    entry_slip, entry_fees = _drag("8.00", "sell", OrderType.market, adv=_ADV_CONTRACTS)
    exit_slip, exit_fees = _drag("3.00", "buy", OrderType.limit, adv=_ADV_CONTRACTS)
    gross = (Decimal("3.00") - Decimal("8.00")) * Decimal(str(_QTY)) * _MULTIPLIER * Decimal(-1)
    expected = signed_money(gross - entry_slip - entry_fees - exit_slip - exit_fees)

    assert result.realized_pl == expected
    assert result.realized_pl > 0


def test_short_loss_is_negative() -> None:
    proposal = _option_proposal(direction="short")
    entry = _entry("3.00")
    brackets = _brackets("8.00", ExitLeg.STOP_HIT)

    result = compute_option_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_contracts=_ADV_CONTRACTS,
        realized_volatility=_RVOL,
    )

    gross = (Decimal("8.00") - Decimal("3.00")) * Decimal(str(_QTY)) * _MULTIPLIER * Decimal(-1)
    entry_slip, entry_fees = _drag("3.00", "sell", OrderType.market, adv=_ADV_CONTRACTS)
    exit_slip, exit_fees = _drag("8.00", "buy", OrderType.stop, adv=_ADV_CONTRACTS)
    expected = signed_money(gross - entry_slip - entry_fees - exit_slip - exit_fees)

    assert result.realized_pl == expected
    assert result.realized_pl < 0


def test_missing_adv_records_zero_slippage_and_fees() -> None:
    proposal = _option_proposal(direction="long")
    entry = _entry("3.00")
    brackets = _brackets("8.00", ExitLeg.TARGET_HIT)

    result = compute_option_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=_CONFIG,
        adv_contracts=None,
        realized_volatility=_RVOL,
    )

    gross = (Decimal("8.00") - Decimal("3.00")) * Decimal(str(_QTY)) * _MULTIPLIER * Decimal(1)
    assert result.realized_pl == signed_money(gross)
    assert result.entry_slippage == Money(Decimal(0))
    assert result.entry_fees == Money(Decimal(0))
    assert result.exit_slippage == Money(Decimal(0))
    assert result.exit_fees == Money(Decimal(0))
