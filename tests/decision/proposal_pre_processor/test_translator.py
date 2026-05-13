"""Tests for the proposal translator — ALP-314.

Each acceptance criterion maps to at least one test. Tests verify behavior
through the public interface only; internal helpers are not tested directly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest

from alphamind._kernel.ids import (
    PositionId,
    RecommendationId,
    Symbol,
    ThesisId,
)
from alphamind.decision.analyst.models import (
    EntryOrder,
    GuardrailValidationResult,
    InstrumentEquity,
    InstrumentOption,
    InstrumentStrategy,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    StrategyLeg,
    Target,
)
from alphamind.decision.proposal_pre_processor.translator import (
    TranslatorError,
    translate_position_assessment_to_proposed_delta,
    translate_recommendation_to_proposed_delta,
)
from alphamind.decision.strategist.models import (
    AddParameters,
    AdjustBracketParameters,
    BracketAdjustNewStopLevel,
    CloseParameters,
    ExposureImpact,
    PositionAssessment,
    ReduceParameters,
)
from alphamind.decision.strategist.models import (
    EntryOrder as StrategistEntryOrder,
)
from alphamind.decision.strategist.models import (
    GuardrailValidationResult as StrategistGuardrailResult,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    ContractType,
    Direction,
    ExistingPosition,
    PortfolioStateSnapshot,
    ProposedDelta,
)

# ---------------------------------------------------------------------------
# Minimal fixture builders — only the fields this story needs
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_EXP = date(2026, 6, 1)


def _snapshot(
    existing_positions: dict[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    return PortfolioStateSnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 5.0, "semis": 0.0, "financials": 0.0, "energy": 0.0}
        ),
        net_long_pct=5.0,
        net_short_pct=0.0,
        gross_pct=5.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType(existing_positions or {}),
    )


def _existing_equity(
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    direction: Direction = Direction.LONG,
    notional_usd: float = 15_000.0,
    quantity: float = 100.0,
    daily_borrow_cost_usd: float | None = None,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=position_id,
        underlying=underlying,
        sector=sector,
        direction=direction,
        asset_type=AssetType.EQUITY,
        notional_usd=notional_usd,
        delta_adjusted_exposure_usd=notional_usd,
        current_greeks=None,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital_usd=0.0,
        quantity=quantity,
    )


def _guardrail_result() -> GuardrailValidationResult:
    return GuardrailValidationResult(overall="PASS", per_rule=(), checked_at=_NOW)


def _invalidation_leg() -> InvalidationLeg:
    return InvalidationLeg(
        leg_id="INV-1",
        type="price",
        is_hard=True,
        condition=PriceCondition(
            underlying_trigger=Symbol("AAPL"), comparator="<=", trigger_price=140.0
        ),
        order_parameters=OrderParameters(order_type="market"),
    )


def _target() -> Target:
    return Target(target_type="absolute_price", price=200.0, dollar_pl_target=5000.0)


def _position_size(
    quantity: float = 100.0,
    dollar_value: float = 15_000.0,
    premium_at_risk: float | None = None,
) -> PositionSize:
    return PositionSize(
        quantity=quantity,
        dollar_value=dollar_value,
        pct_of_portfolio=15.0,
        premium_at_risk=premium_at_risk,
    )


def _equity_recommendation(
    rec_id: str = "REC-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    direction: str = "long",
    quantity: float = 100.0,
    dollar_value: float = 15_000.0,
    order_type: str = "market",
) -> Recommendation:
    entry_order_kwargs: dict[str, object] = {"type": order_type}
    if order_type in ("limit", "stop_limit"):
        entry_order_kwargs["limit_price"] = 155.0
    if order_type == "stop_limit":
        entry_order_kwargs["stop_price"] = 150.0
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentEquity(asset_type="equity", ticker=underlying, direction=direction),  # type: ignore[arg-type]
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        conviction_level=3,
        entry_order=EntryOrder(**entry_order_kwargs),  # type: ignore[arg-type]
        position_size=_position_size(quantity=quantity, dollar_value=dollar_value),
        target=_target(),
        invalidation_legs=(_invalidation_leg(),),
        guardrail_validation_result=_guardrail_result(),
        thesis_narrative="Test thesis",
        target_rationale="Test target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="Test"),),
        position_size_rationale="Test sizing",
        counterarguments_acknowledged="None",
        time_expectation_hours=24.0,
    )


# ===========================================================================
# AC-1: Long equity recommendation round-trip
# ===========================================================================


def test_long_equity_recommendation_round_trip() -> None:
    """AC-1: long equity → direction=LONG, asset_type=EQUITY, action=OPEN, correct notional/qty."""
    rec = _equity_recommendation(
        direction="long",
        quantity=100.0,
        dollar_value=15_000.0,
    )
    snap = _snapshot()
    delta = translate_recommendation_to_proposed_delta(rec, snapshot=snap)

    assert isinstance(delta, ProposedDelta)
    assert delta.id == "REC-1"
    assert delta.underlying == "AAPL"
    assert delta.sector == "tech"
    assert delta.direction == Direction.LONG
    assert delta.asset_type == AssetType.EQUITY
    assert delta.notional_usd == 15_000.0
    assert delta.quantity == 100.0
    assert delta.option_legs is None
    assert delta.action == Action.OPEN
    assert delta.existing_position_id is None
    assert delta.daily_borrow_cost_usd is None
    assert delta.reserves_capital is False


def _option_recommendation(
    rec_id: str = "REC-2",
    underlying: str = "AAPL",
    sector: str = "tech",
    direction: str = "long",
    strike: float = 150.0,
    contract_type: str = "call",
    quantity: float = 5.0,
    dollar_value: float = 2_000.0,
    premium_at_risk: float | None = 750.0,
) -> Recommendation:
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentOption(
            asset_type="option",
            underlying=Symbol(underlying),
            strike=strike,
            expiration=_EXP,
            contract_type=contract_type,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
        ),
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        conviction_level=3,
        entry_order=EntryOrder(type="market"),
        position_size=_position_size(
            quantity=quantity, dollar_value=dollar_value, premium_at_risk=premium_at_risk
        ),
        target=_target(),
        invalidation_legs=(_invalidation_leg(),),
        guardrail_validation_result=_guardrail_result(),
        thesis_narrative="Test thesis",
        target_rationale="Test target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="Test"),),
        position_size_rationale="Test sizing",
        counterarguments_acknowledged="None",
        time_expectation_hours=24.0,
    )


def _strategy_recommendation(
    rec_id: str = "REC-3",
    underlying: str = "AAPL",
    sector: str = "tech",
    quantity: float = 3.0,
    dollar_value: float = 1_000.0,
    premium_at_risk: float | None = 600.0,
) -> Recommendation:
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentStrategy(
            asset_type="strategy",
            strategy_type="vertical_spread",
            underlying=Symbol(underlying),
            legs=(
                StrategyLeg(
                    strike=150.0,
                    expiration=_EXP,
                    contract_type="call",
                    direction="long",
                    quantity_ratio=1,
                ),
                StrategyLeg(
                    strike=160.0,
                    expiration=_EXP,
                    contract_type="call",
                    direction="short",
                    quantity_ratio=1,
                ),
            ),
        ),
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        conviction_level=3,
        entry_order=EntryOrder(type="market"),
        position_size=_position_size(
            quantity=quantity, dollar_value=dollar_value, premium_at_risk=premium_at_risk
        ),
        target=_target(),
        invalidation_legs=(_invalidation_leg(),),
        guardrail_validation_result=_guardrail_result(),
        thesis_narrative="Test thesis",
        target_rationale="Test target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="Test"),),
        position_size_rationale="Test sizing",
        counterarguments_acknowledged="None",
        time_expectation_hours=24.0,
    )


# ===========================================================================
# AC-2: Long call option recommendation → option_legs populated
# ===========================================================================


def test_long_call_option_recommendation() -> None:
    """AC-2: long call option → OPTION asset_type, one-leg option_legs, premium_at_risk notional."""
    rec = _option_recommendation(
        direction="long",
        strike=150.0,
        contract_type="call",
        quantity=5.0,
        premium_at_risk=750.0,
    )
    snap = _snapshot()
    delta = translate_recommendation_to_proposed_delta(rec, snapshot=snap)

    assert delta.asset_type == AssetType.OPTION
    assert delta.notional_usd == 750.0  # premium_at_risk
    assert delta.option_legs is not None
    assert len(delta.option_legs) == 1
    leg = delta.option_legs[0]
    assert leg.contract_type == ContractType.CALL
    assert leg.strike == 150.0
    assert leg.expiration == _EXP
    assert leg.quantity == 5  # positive (long)
    assert delta.action == Action.OPEN
    assert delta.direction == Direction.LONG


# ===========================================================================
# AC-3: Multi-leg strategy recommendation → option_legs matches legs count
# ===========================================================================


def test_strategy_recommendation_legs() -> None:
    """AC-3: vertical spread → 2 option_legs, quantities signed by leg direction."""
    rec = _strategy_recommendation(quantity=3.0, premium_at_risk=600.0)
    snap = _snapshot()
    delta = translate_recommendation_to_proposed_delta(rec, snapshot=snap)

    assert delta.asset_type == AssetType.STRATEGY
    assert delta.notional_usd == 600.0
    assert delta.option_legs is not None
    assert len(delta.option_legs) == 2
    long_leg, short_leg = delta.option_legs
    assert long_leg.quantity == 3  # long direction → positive
    assert short_leg.quantity == -3  # short direction → negative


# ===========================================================================
# AC-4 & AC-5: Short equity borrow cost
# ===========================================================================


def test_short_equity_with_snapshot_picks_up_borrow_cost() -> None:
    """AC-4: SHORT EQUITY with matching snapshot position picks up daily_borrow_cost_usd."""
    existing = _existing_equity(
        position_id="POS-SHORT-1",
        underlying="NVDA",
        sector="semis",
        direction=Direction.SHORT,
        daily_borrow_cost_usd=12.50,
    )
    snap = _snapshot(existing_positions={"POS-SHORT-1": existing})
    rec = _equity_recommendation(
        rec_id="REC-10",
        underlying="NVDA",
        sector="semis",
        direction="short",
    )
    delta = translate_recommendation_to_proposed_delta(rec, snapshot=snap)

    assert delta.direction == Direction.SHORT
    assert delta.daily_borrow_cost_usd == 12.50


def test_short_equity_no_snapshot_raises_translator_error() -> None:
    """AC-5: SHORT EQUITY with no matching short in snapshot raises TranslatorError."""
    snap = _snapshot()  # empty existing_positions
    rec = _equity_recommendation(
        rec_id="REC-11",
        underlying="NVDA",
        sector="semis",
        direction="short",
    )
    with pytest.raises(TranslatorError, match="borrow cost unavailable"):
        translate_recommendation_to_proposed_delta(rec, snapshot=snap)


# ===========================================================================
# AC-6: reserves_capital from entry order type
# ===========================================================================


def test_limit_order_reserves_capital() -> None:
    """AC-6a: limit entry order → reserves_capital=True."""
    rec = _equity_recommendation(order_type="limit")
    snap = _snapshot()
    delta = translate_recommendation_to_proposed_delta(rec, snapshot=snap)
    assert delta.reserves_capital is True


def test_market_order_does_not_reserve_capital() -> None:
    """AC-6b: market entry order → reserves_capital=False."""
    rec = _equity_recommendation(order_type="market")
    snap = _snapshot()
    delta = translate_recommendation_to_proposed_delta(rec, snapshot=snap)
    assert delta.reserves_capital is False


# ===========================================================================
# Strategist assessment fixture helpers
# ===========================================================================


def _exposure_impact() -> ExposureImpact:
    return ExposureImpact(sector_delta_adjusted_change=-5000.0, net_directional_impact=-5000.0)


def _strategist_guardrail() -> StrategistGuardrailResult:
    return StrategistGuardrailResult(overall="PASS", per_rule=(), checked_at=_NOW)


def _close_assessment(
    sa_id: str = "SA-1",
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    quantity: float | str = "all",
) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THESIS-1"),
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        thesis_status="on-track",
        recommended_action="close",
        action_parameters=CloseParameters(
            action="close",
            quantity=quantity,  # type: ignore[arg-type]
            order_type="market",
            close_rationale_type="target_reached",
        ),
        exposure_impact=_exposure_impact(),
        status_rationale="Target reached",
        action_rationale="Close position",
    )


def _reduce_assessment(
    sa_id: str = "SA-2",
    position_id: str = "POS-1",
    quantity: float = 30.0,
) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THESIS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        thesis_status="at-risk",
        recommended_action="reduce",
        action_parameters=ReduceParameters(
            action="reduce",
            quantity=quantity,
            order_type="market",
        ),
        exposure_impact=_exposure_impact(),
        reduce_rationale="Reducing risk",
        status_rationale="At risk",
        action_rationale="Reduce position",
    )


def _add_assessment(
    sa_id: str = "SA-3",
    position_id: str = "POS-1",
    additional_quantity: float = 50.0,
    additional_dollar_value: float = 7_500.0,
) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THESIS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        thesis_status="on-track",
        recommended_action="add",
        action_parameters=AddParameters(
            action="add",
            additional_quantity=additional_quantity,
            additional_dollar_value=additional_dollar_value,
            entry_order=StrategistEntryOrder(type="market"),
        ),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=7500.0, net_directional_impact=7500.0
        ),
        guardrail_validation_result=_strategist_guardrail(),
        add_conviction_justification="Strong conviction",
        status_rationale="On track",
        action_rationale="Adding to winner",
    )


def _adjust_bracket_assessment(
    sa_id: str = "SA-4",
    position_id: str = "POS-1",
) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THESIS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        thesis_status="on-track",
        recommended_action="adjust-bracket",
        action_parameters=AdjustBracketParameters(
            action="adjust-bracket",
            new_stop_level=BracketAdjustNewStopLevel(
                trigger_price=140.0,
                order_type="market",
            ),
        ),
        adjustment_rationale="Tighten stop",
        status_rationale="On track",
        action_rationale="Adjust bracket",
    )


def _hold_assessment(sa_id: str = "SA-5", position_id: str = "POS-1") -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THESIS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        thesis_status="on-track",
        recommended_action="hold",
        status_rationale="On track",
        action_rationale="Hold",
    )


def _snapshot_with_position(
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    notional_usd: float = 15_000.0,
    quantity: float = 100.0,
    direction: Direction = Direction.LONG,
) -> PortfolioStateSnapshot:
    existing = _existing_equity(
        position_id=position_id,
        underlying=underlying,
        notional_usd=notional_usd,
        quantity=quantity,
        direction=direction,
    )
    return _snapshot(existing_positions={position_id: existing})


# ===========================================================================
# AC-7: Close "all" → full position size
# ===========================================================================


def test_close_all_uses_full_position_size() -> None:
    """AC-7: close with quantity='all' → quantity and notional match existing position."""
    snap = _snapshot_with_position(notional_usd=15_000.0, quantity=100.0)
    assessment = _close_assessment(quantity="all")
    delta = translate_position_assessment_to_proposed_delta(assessment, snapshot=snap)

    assert delta.action == Action.CLOSE
    assert delta.quantity == 100.0
    assert delta.notional_usd == 15_000.0
    assert delta.existing_position_id == "POS-1"
    assert delta.reserves_capital is False
    assert delta.daily_borrow_cost_usd is None


# ===========================================================================
# AC-8: Reduce → Action.CLOSE with partial quantity / pro-rated notional
# ===========================================================================


def test_reduce_emits_close_with_partial_notional() -> None:
    """AC-8: reduce action → Action.CLOSE, partial qty, pro-rated notional."""
    snap = _snapshot_with_position(notional_usd=15_000.0, quantity=100.0)
    assessment = _reduce_assessment(quantity=30.0)
    delta = translate_position_assessment_to_proposed_delta(assessment, snapshot=snap)

    assert delta.action == Action.CLOSE
    assert delta.quantity == 30.0
    assert abs(delta.notional_usd - 4_500.0) < 0.01  # 30/100 * 15000


# ===========================================================================
# AC-9: Adjust-bracket → Action.ADJUST with 0/0
# ===========================================================================


def test_adjust_bracket_emits_adjust_with_zero_exposure() -> None:
    """AC-9: adjust-bracket → Action.ADJUST, notional=0.0, quantity=0.0."""
    snap = _snapshot_with_position()
    assessment = _adjust_bracket_assessment()
    delta = translate_position_assessment_to_proposed_delta(assessment, snapshot=snap)

    assert delta.action == Action.ADJUST
    assert delta.notional_usd == 0.0
    assert delta.quantity == 0.0
    assert delta.existing_position_id == "POS-1"


# ===========================================================================
# AC-10: Add → Action.ADD with additional quantities
# ===========================================================================


def test_add_emits_add_with_additional_quantities() -> None:
    """AC-10: add action → Action.ADD, additional qty/notional, existing_position_id."""
    snap = _snapshot_with_position()
    assessment = _add_assessment(additional_quantity=50.0, additional_dollar_value=7_500.0)
    delta = translate_position_assessment_to_proposed_delta(assessment, snapshot=snap)

    assert delta.action == Action.ADD
    assert delta.quantity == 50.0
    assert delta.notional_usd == 7_500.0
    assert delta.existing_position_id == "POS-1"


# ===========================================================================
# AC-11: Hold → TranslatorError
# ===========================================================================


def test_hold_assessment_raises_translator_error() -> None:
    """AC-11: hold-action assessment raises TranslatorError."""
    snap = _snapshot_with_position()
    assessment = _hold_assessment()
    with pytest.raises(TranslatorError, match="hold-action"):
        translate_position_assessment_to_proposed_delta(assessment, snapshot=snap)


# ===========================================================================
# AC-12: Missing position_id → TranslatorError
# ===========================================================================


def test_missing_position_id_raises_translator_error() -> None:
    """AC-12: position_id not in snapshot raises TranslatorError."""
    snap = _snapshot()  # empty existing_positions
    assessment = _close_assessment(position_id="POS-MISSING")
    with pytest.raises(TranslatorError, match="POS-MISSING"):
        translate_position_assessment_to_proposed_delta(assessment, snapshot=snap)


# ===========================================================================
# AC-13: Pair-test — translator output accepted by evaluate_proposals
# ===========================================================================


def test_translator_output_accepted_by_evaluate_proposals() -> None:
    """AC-13: translated ProposedDeltas pass evaluate_proposals without LibraryInputError."""
    from alphamind.risk_guardrails.guardrail_evaluation import (
        EscalationZones,
        FeatureFlagsView,
        FixtureIvProvider,
        IvQuote,
        IvSurfaceEntry,
        LibraryConfig,
        LibraryInputError,
        MarketInputs,
        evaluate_proposals,
    )

    as_of = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
    expiration = date(2026, 5, 28)
    spot = 150.0
    underlying = "AAPL"

    # Build a minimal snapshot with one existing LONG EQUITY position
    existing = _existing_equity(
        position_id="POS-1",
        underlying=underlying,
        notional_usd=15_000.0,
        quantity=100.0,
        direction=Direction.LONG,
    )
    snap = PortfolioStateSnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 15.0, "semis": 0.0, "financials": 0.0, "energy": 0.0}
        ),
        net_long_pct=15.0,
        net_short_pct=0.0,
        gross_pct=15.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({"POS-1": existing}),
    )

    zones = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
    effective_limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 40.0,
        "gross_exposure_pct": 100.0,
        "options_delta_pct": 30.0,
        "portfolio_theta_pct_per_day": 0.5,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    lib_config = LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: zones for k in effective_limits}),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )

    provider = FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
                quotes=(
                    IvQuote(
                        strike=150.0,
                        expiration=expiration,
                        contract_type=ContractType.CALL,
                        implied_volatility=0.30,
                    ),
                ),
            )
        },
        realized_vol={},
    )
    market = MarketInputs(
        underlying_prices={underlying: spot},
        risk_free_rate=0.045,
        iv_provider=provider,
        as_of=as_of,
    )

    # Translate a long equity OPEN and a CLOSE
    rec = _equity_recommendation(
        rec_id="REC-1",
        underlying=underlying,
        sector="tech",
        direction="long",
        quantity=50.0,
        dollar_value=7_500.0,
    )
    open_delta = translate_recommendation_to_proposed_delta(rec, snapshot=snap)

    close_assessment = _close_assessment(
        sa_id="SA-1", position_id="POS-1", underlying=underlying, sector="tech", quantity="all"
    )
    close_delta = translate_position_assessment_to_proposed_delta(close_assessment, snapshot=snap)

    # Both should be accepted without raising LibraryInputError
    try:
        evaluate_proposals(
            state=snap,
            proposals=[open_delta, close_delta],
            config=lib_config,
            market=market,
        )
    except LibraryInputError as exc:
        pytest.fail(f"evaluate_proposals raised LibraryInputError: {exc}")
