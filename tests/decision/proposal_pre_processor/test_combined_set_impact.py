"""Tests for combined-set impact + breach contributors — ALP-317.

Each acceptance criterion maps to at least one test. Tests verify behavior
through the public function ``compute_combined_set_impact`` only.

Fixtures borrow the translator-test pattern (recommendations, position
assessments, snapshots) and the evaluate-proposals fixture pattern (full
config, IV provider, market) so the projection layer is exercised end-to-end.
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
from alphamind._kernel.money import money, price, signed_money
from alphamind.decision.analyst.models import (
    EntryOrder,
    GuardrailValidationResult,
    InstrumentEquity,
    InstrumentOption,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    Target,
)
from alphamind.decision.proposal_pre_processor.models import (
    CombinedSetImpact,
)
from alphamind.decision.proposal_pre_processor.observations import (
    LibraryFeatureDisabledError,
    compute_combined_set_impact,
)
from alphamind.decision.proposal_pre_processor.translator import (
    translate_recommendation_to_proposed_delta,
)
from alphamind.decision.strategist.models import (
    CloseParameters,
    ExposureImpact,
    PositionAssessment,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    AssetType,
    ContractType,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
    build_active_specs,
    compute_delta_adjusted_exposure,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXP = date(2026, 5, 28)
_SPOT = 150.0


# ---------------------------------------------------------------------------
# Snapshot / config / market builders
# ---------------------------------------------------------------------------


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _full_config(
    *,
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
    net_long_limit: float = 60.0,
    min_cash_limit: float = 10.0,
) -> LibraryConfig:
    """All 13 in-scope rules enabled — same shape used in test_evaluate_proposals."""
    effective_limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": net_long_limit,
        "net_short_pct": 40.0,
        "gross_exposure_pct": 100.0,
        "options_delta_pct": 30.0,
        "portfolio_theta_pct_per_day": 0.5,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": min_cash_limit,
        "pending_order_capital_pct": 20.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _market(underlying: str = "AAPL") -> MarketInputs:
    provider = FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
                quotes=(
                    IvQuote(
                        strike=150.0,
                        expiration=_EXP,
                        contract_type=ContractType.CALL,
                        implied_volatility=0.30,
                    ),
                ),
            )
        },
        realized_vol={},
    )
    return MarketInputs(
        underlying_prices={underlying: _SPOT, "NVDA": _SPOT, "ABC": _SPOT},
        risk_free_rate=0.045,
        iv_provider=provider,
        as_of=_NOW,
    )


def _existing_long_equity(
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    notional_usd: float = 15_000.0,
    quantity: float = 100.0,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=position_id,
        underlying=underlying,
        sector=sector,
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=notional_usd,
        delta_adjusted_exposure_usd=notional_usd,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
        quantity=quantity,
    )


def _snapshot(
    *,
    portfolio_value_usd: float = 100_000.0,
    cash_usd: float = 70_000.0,
    sector_exposure_pct: dict[str, float] | None = None,
    net_long_pct: float = 30.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 30.0,
    position_max_size_pct: float = 5.0,
    existing_positions: dict[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {"tech": 15.0, "semis": 0.0, "financials": 0.0, "energy": 0.0}
    if existing_positions is None:
        # ALP-621: the position_max_size_pct rule projects from the simulated
        # post-batch book. Synthesize a position consistent with the scalar
        # field so the rule's projected_after matches current when no
        # proposals modify it. Tests that need a specific book pass
        # existing_positions explicitly.
        if position_max_size_pct > 0.0 and portfolio_value_usd > 0.0:
            synth_notional = position_max_size_pct / 100.0 * portfolio_value_usd
            existing_positions = {
                "POS-SYNTH": ExistingPosition(
                    position_id=PositionId("POS-SYNTH"),
                    underlying=Symbol("AAPL"),
                    sector="tech",
                    direction=Direction.LONG,
                    asset_type=AssetType.EQUITY,
                    notional_usd=synth_notional,
                    delta_adjusted_exposure_usd=synth_notional,
                    current_greeks=None,
                    daily_borrow_cost_usd=None,
                    reserves_capital_usd=0.0,
                )
            }
        else:
            existing_positions = {}
    return PortfolioStateSnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(sector_exposure_pct),
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        gross_pct=gross_pct,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=position_max_size_pct,
        existing_positions=MappingProxyType(existing_positions),
    )


# ---------------------------------------------------------------------------
# Recommendation / assessment builders
# ---------------------------------------------------------------------------


def _guardrail_result() -> GuardrailValidationResult:
    return GuardrailValidationResult(overall="PASS", per_rule=(), checked_at=_NOW)


def _invalidation_leg() -> InvalidationLeg:
    return InvalidationLeg(
        leg_id="INV-1",
        type="price",
        is_hard=True,
        condition=PriceCondition(
            underlying_trigger=Symbol("AAPL"), comparator="<=", trigger_price=price(140.0)
        ),
        order_parameters=OrderParameters(order_type="market"),
    )


def _equity_recommendation(
    *,
    rec_id: str = "REC-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    direction: str = "long",
    quantity: float = 100.0,
    dollar_value: float = 15_000.0,
) -> Recommendation:
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentEquity(asset_type="equity", ticker=underlying, direction=direction),  # type: ignore[arg-type]
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        conviction_level=3,
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(
            quantity=quantity, dollar_value=money(dollar_value), pct_of_portfolio=15.0
        ),
        target=Target(
            target_type="absolute_price", price=price(200.0), dollar_pl_target=money(5000.0)
        ),
        invalidation_legs=(_invalidation_leg(),),
        guardrail_validation_result=_guardrail_result(),
        thesis_narrative="Test thesis",
        target_rationale="Test target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="Test"),),
        position_size_rationale="Test sizing",
        counterarguments_acknowledged="None",
        time_expectation_hours=24.0,
    )


def _close_assessment(
    *,
    sa_id: str = "SA-1",
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
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
            quantity="all",
            order_type="market",
            close_rationale_type="target_reached",
        ),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=signed_money(-15_000.0),
            net_directional_impact=signed_money(-15_000.0),
        ),
        status_rationale="Target reached",
        action_rationale="Close",
    )


# ===========================================================================
# AC: Empty inputs produce a baseline projection with no breaches
# ===========================================================================


def test_empty_inputs_produce_baseline_combined_set_impact() -> None:
    """Zero recommendations + zero non-hold assessments → empty IDs, no breaches.

    per_rule reflects the snapshot baseline (no proposed changes).
    """
    snap = _snapshot()
    config = _full_config()
    market = _market()

    result = compute_combined_set_impact(
        recommendations=(),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    assert isinstance(result, CombinedSetImpact)
    assert result.basis.analyst_proposal_ids == ()
    assert result.basis.strategist_action_ids == ()
    assert result.basis.strategist_holds_excluded_count == 0
    assert result.basis.snapshot_timestamp == _NOW
    assert result.breaches == ()
    # per_rule reflects the snapshot baseline — every projected_after equals current
    assert len(result.per_rule) > 0
    for entry in result.per_rule:
        assert entry.current == entry.projected_after, (
            f"rule {entry.rule}: zero proposals must keep projected_after == current"
        )


# ===========================================================================
# AC: Single equity recommendation that does NOT breach any rule
# ===========================================================================


def test_single_non_breaching_recommendation_has_no_breaches() -> None:
    """One small equity recommendation under all limits → empty breaches, all PASS/WARNING."""
    snap = _snapshot()
    config = _full_config()
    market = _market()
    rec = _equity_recommendation(quantity=20.0, dollar_value=3_000.0)  # 3% of portfolio

    result = compute_combined_set_impact(
        recommendations=(rec,),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    assert result.basis.analyst_proposal_ids == ("REC-1",)
    assert result.breaches == ()
    for entry in result.per_rule:
        assert entry.status in ("PASS", "WARNING"), (
            f"rule {entry.rule} unexpectedly FAIL with status {entry.status}"
        )


# ===========================================================================
# AC: Single recommendation breaching net_long_pct → breach with positive contributor
# ===========================================================================


def test_single_breaching_recommendation_records_positive_contributor() -> None:
    """An equity OPEN that drives net_long over the hard-block records the rec as a contributor.

    State: net_long=30, limit=40, hard_block=95% → FAIL when projected >= 38.
    Rec: $15k long equity = 15% of portfolio → projected = 30 + 15 = 45 → FAIL.
    Contribution to net_long_pct should equal 15.0 (signed positive).
    """
    snap = _snapshot(
        net_long_pct=30.0,
        gross_pct=30.0,
        sector_exposure_pct={"tech": 30.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
    )
    config = _full_config(net_long_limit=40.0)
    market = _market()
    rec = _equity_recommendation(quantity=100.0, dollar_value=15_000.0)

    result = compute_combined_set_impact(
        recommendations=(rec,),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    breaches_by_rule = {b.rule: b for b in result.breaches}
    assert "net_long_pct" in breaches_by_rule, (
        f"expected net_long_pct breach; got rules: {list(breaches_by_rule)}"
    )
    breach = breaches_by_rule["net_long_pct"]
    assert breach.overage > 0.0
    contributor_ids = {c.proposal_id for c in breach.contributors}
    assert "REC-1" in contributor_ids
    rec_contribution = next(c for c in breach.contributors if c.proposal_id == "REC-1")
    assert rec_contribution.contribution > 0.0
    # Contribution ≈ 15% (after delta-buffer scaling: equity is 1.0 delta, no buffer applied
    # to absolute notional for equity → ~15.0)
    assert abs(rec_contribution.contribution - 15.0) < 0.5


# ===========================================================================
# AC: Combined set with two recs and one close → 3 contributors (2 positive, 1 negative)
# ===========================================================================


def test_combined_set_two_recs_one_close_records_signed_contributors() -> None:
    """Two opening recs push net_long over the limit; one close pulls it down.

    Net_long state: 30. Limit: 40. hard_block at 38.
    Rec-1 (long, 12k) + Rec-2 (long, 10k) → +22%. Close (existing 5k long) → -5%.
    Projected: 30 + 22 - 5 = 47% → FAIL. All three should appear as contributors.
    """
    existing = _existing_long_equity(
        position_id=PositionId("POS-1"),
        underlying=Symbol("AAPL"),
        notional_usd=5_000.0,
        quantity=33.0,
    )
    snap = _snapshot(
        net_long_pct=30.0,
        gross_pct=30.0,
        sector_exposure_pct={"tech": 30.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
        existing_positions={"POS-1": existing},
    )
    config = _full_config(net_long_limit=40.0)
    market = _market()
    rec_1 = _equity_recommendation(rec_id="REC-1", quantity=80.0, dollar_value=12_000.0)
    rec_2 = _equity_recommendation(rec_id="REC-2", quantity=66.0, dollar_value=10_000.0)
    close = _close_assessment(sa_id="SA-1", position_id=PositionId("POS-1"))

    result = compute_combined_set_impact(
        recommendations=(rec_1, rec_2),
        non_hold_position_assessments=(close,),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    breaches_by_rule = {b.rule: b for b in result.breaches}
    assert "net_long_pct" in breaches_by_rule
    breach = breaches_by_rule["net_long_pct"]
    by_id = {c.proposal_id: c.contribution for c in breach.contributors}
    assert "REC-1" in by_id and by_id["REC-1"] > 0.0
    assert "REC-2" in by_id and by_id["REC-2"] > 0.0
    assert "SA-1" in by_id and by_id["SA-1"] < 0.0  # close pulls away


# ===========================================================================
# AC: Inverse rule FAIL → overage = limit - projected_after (positive)
# ===========================================================================


def test_inverse_rule_breach_overage_uses_floor_minus_projected() -> None:
    """Cash drops below the floor → min_cash_reserve_pct breaches; overage is positive.

    Cash baseline 8k = 8% (already below 10% limit). Recommendation buys 5k → projected
    cash = 3% → FAIL. overage = 10 - 3 = 7 (positive).
    """
    snap = _snapshot(
        cash_usd=8_000.0,
        net_long_pct=30.0,
        gross_pct=30.0,
        sector_exposure_pct={"tech": 30.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
    )
    # Bump net_long_limit so we don't accidentally trip net_long alongside cash.
    config = _full_config(net_long_limit=80.0, min_cash_limit=10.0)
    market = _market()
    rec = _equity_recommendation(quantity=33.0, dollar_value=5_000.0)

    result = compute_combined_set_impact(
        recommendations=(rec,),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    breaches_by_rule = {b.rule: b for b in result.breaches}
    assert "min_cash_reserve_pct" in breaches_by_rule
    breach = breaches_by_rule["min_cash_reserve_pct"]
    # Confirm matching per_rule entry has FAIL status with limit > projected_after
    per_rule_by_rule = {p.rule: p for p in result.per_rule}
    cash_entry = per_rule_by_rule["min_cash_reserve_pct"]
    assert cash_entry.status == "FAIL"
    assert cash_entry.projected_after < cash_entry.limit
    # Overage on inverse rule is the gap below the floor: limit - projected_after.
    expected_overage = cash_entry.limit - cash_entry.projected_after
    assert abs(breach.overage - expected_overage) < 1e-9
    assert breach.overage > 0.0  # positive even though projection is below the floor


# ===========================================================================
# AC: Zero-contribution proposals are dropped from contributors[]
# ===========================================================================


def test_zero_contribution_proposals_excluded_from_contributors() -> None:
    """Proposals contributing 0.0 to a breaching rule do not appear in contributors[].

    The sector_concentration_<sector> rule per-proposal contribution is 0.0 unless
    the proposal's sector matches the rule's sector. A semis-sector recommendation
    contributing to a tech-sector breach would be a contract violation; verify it's
    dropped.
    """
    snap = _snapshot(
        net_long_pct=10.0,
        gross_pct=20.0,
        sector_exposure_pct={"tech": 22.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
    )
    config = _full_config()
    market = _market()
    # Tech rec pushes sector_concentration_tech toward FAIL (limit 25, hard_block at 23.75).
    tech_rec = _equity_recommendation(
        rec_id="REC-1", sector="tech", quantity=20.0, dollar_value=3_000.0
    )
    # Semis rec contributes 0.0 to sector_concentration_tech.
    semis_rec = _equity_recommendation(
        rec_id="REC-2",
        underlying=Symbol("NVDA"),
        sector="semis",
        quantity=20.0,
        dollar_value=2_000.0,
    )

    result = compute_combined_set_impact(
        recommendations=(tech_rec, semis_rec),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    breaches_by_rule = {b.rule: b for b in result.breaches}
    assert "sector_concentration_tech" in breaches_by_rule
    tech_breach = breaches_by_rule["sector_concentration_tech"]
    contributor_ids = {c.proposal_id for c in tech_breach.contributors}
    assert "REC-1" in contributor_ids
    assert "REC-2" not in contributor_ids  # zero-contribution to this rule, dropped


# ===========================================================================
# AC: Feature-disabled rejections are surfaced (not papered over)
# ===========================================================================


def test_feature_disabled_proposals_raise() -> None:
    """If the library returns feature_disabled rejections, the function raises.

    Construct an option recommendation but disable options in the library config —
    the feature gate flags the proposal, the function surfaces it.
    """
    snap = _snapshot()
    config = _full_config(options_enabled=False)
    market = _market()
    option_rec = Recommendation(
        recommendation_id=RecommendationId("REC-1"),
        instrument=InstrumentOption(
            asset_type="option",
            underlying=Symbol("AAPL"),
            strike=price(150.0),
            expiration=_EXP,
            contract_type="call",
            direction="long",
        ),
        underlying=Symbol("AAPL"),
        sector="tech",
        conviction_level=3,
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(
            quantity=5.0,
            dollar_value=money(2_000.0),
            pct_of_portfolio=2.0,
            premium_at_risk=money(750.0),
        ),
        target=Target(
            target_type="absolute_price", price=price(200.0), dollar_pl_target=money(5000.0)
        ),
        invalidation_legs=(_invalidation_leg(),),
        guardrail_validation_result=_guardrail_result(),
        thesis_narrative="Test thesis",
        target_rationale="Test target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="Test"),),
        position_size_rationale="Test sizing",
        counterarguments_acknowledged="None",
        time_expectation_hours=24.0,
    )

    with pytest.raises(LibraryFeatureDisabledError, match="REC-1"):
        compute_combined_set_impact(
            recommendations=(option_rec,),
            non_hold_position_assessments=(),
            snapshot=snap,
            library_config=config,
            market=market,
            snapshot_timestamp=_NOW,
            strategist_holds_excluded_count=0,
        )


# ===========================================================================
# AC: function is pure -- same inputs produce equal outputs
# ===========================================================================


def test_repeated_calls_with_same_inputs_produce_equal_outputs() -> None:
    """Same inputs → same outputs across two calls."""
    snap = _snapshot(
        net_long_pct=30.0,
        gross_pct=30.0,
        sector_exposure_pct={"tech": 30.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
    )
    config = _full_config(net_long_limit=40.0)
    market = _market()
    rec = _equity_recommendation(quantity=100.0, dollar_value=15_000.0)

    first = compute_combined_set_impact(
        recommendations=(rec,),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )
    second = compute_combined_set_impact(
        recommendations=(rec,),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )
    assert first == second


# ===========================================================================
# AC: evaluate_proposals is called exactly once (not per proposal)
# ===========================================================================


def test_evaluate_proposals_invoked_once_for_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The library is called once with all proposals — not once per proposal."""
    from alphamind.decision.proposal_pre_processor import observations as observations_module
    from alphamind.risk_guardrails.guardrail_evaluation import (
        LibraryConfig as RealLibraryConfig,
    )
    from alphamind.risk_guardrails.guardrail_evaluation import (
        LibraryOutput,
        evaluate_proposals,
    )
    from alphamind.risk_guardrails.guardrail_evaluation import (
        MarketInputs as RealMarketInputs,
    )
    from alphamind.risk_guardrails.guardrail_evaluation import (
        PortfolioStateSnapshot as RealSnapshot,
    )
    from alphamind.risk_guardrails.guardrail_evaluation import (
        ProposedDelta as RealProposedDelta,
    )

    call_count = 0

    def counting_evaluate(
        *,
        state: RealSnapshot,
        proposals: tuple[RealProposedDelta, ...],
        config: RealLibraryConfig,
        market: RealMarketInputs,
    ) -> LibraryOutput:
        nonlocal call_count
        call_count += 1
        return evaluate_proposals(state=state, proposals=proposals, config=config, market=market)

    monkeypatch.setattr(observations_module, "evaluate_proposals", counting_evaluate)

    snap = _snapshot(
        net_long_pct=10.0,
        gross_pct=20.0,
        sector_exposure_pct={"tech": 20.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
    )
    config = _full_config()
    market = _market()
    recs = (
        _equity_recommendation(rec_id="REC-1", quantity=20.0, dollar_value=2_000.0),
        _equity_recommendation(rec_id="REC-2", quantity=20.0, dollar_value=2_000.0),
        _equity_recommendation(rec_id="REC-3", quantity=20.0, dollar_value=2_000.0),
    )

    compute_combined_set_impact(
        recommendations=recs,
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    assert call_count == 1, f"evaluate_proposals called {call_count} times; expected 1"


# ===========================================================================
# AC: Spec lookup raises KeyError if a FAIL rule's rule_id is missing
# ===========================================================================


def test_missing_rule_spec_raises_key_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a FAIL rule has no matching RuleSpec, the function raises KeyError.

    Stub ``build_active_specs`` to return an empty registry; the projection layer
    still produces FAIL outcomes via the unmocked path inside ``evaluate_proposals``,
    but the contributor lookup cannot resolve the rule.
    """
    from alphamind.decision.proposal_pre_processor import observations as observations_module

    monkeypatch.setattr(observations_module, "build_active_specs", lambda config: ())

    snap = _snapshot(
        net_long_pct=30.0,
        gross_pct=30.0,
        sector_exposure_pct={"tech": 30.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
    )
    config = _full_config(net_long_limit=40.0)
    market = _market()
    rec = _equity_recommendation(quantity=100.0, dollar_value=15_000.0)

    with pytest.raises(KeyError):
        compute_combined_set_impact(
            recommendations=(rec,),
            non_hold_position_assessments=(),
            snapshot=snap,
            library_config=config,
            market=market,
            snapshot_timestamp=_NOW,
            strategist_holds_excluded_count=0,
        )


# ===========================================================================
# Verification gate: contributor.contribution matches spec.contribute() output
# ===========================================================================


def test_contributor_attribution_matches_rule_spec_contribute() -> None:
    """For each contributor, the recorded value equals ``spec.contribute(...)`` exactly.

    Construct a fixture where exactly one rule fails under a known proposal set,
    then assert each contributor's signed contribution matches the spec's
    individual contribute() output computed independently.
    """
    snap = _snapshot(
        net_long_pct=30.0,
        gross_pct=30.0,
        sector_exposure_pct={"tech": 30.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
    )
    config = _full_config(net_long_limit=40.0)
    market = _market()
    rec_1 = _equity_recommendation(rec_id="REC-1", quantity=80.0, dollar_value=12_000.0)
    rec_2 = _equity_recommendation(rec_id="REC-2", quantity=66.0, dollar_value=10_000.0)

    result = compute_combined_set_impact(
        recommendations=(rec_1, rec_2),
        non_hold_position_assessments=(),
        snapshot=snap,
        library_config=config,
        market=market,
        snapshot_timestamp=_NOW,
        strategist_holds_excluded_count=0,
    )

    # Recompute net_long contributions independently using the same library primitives.
    net_long_spec = next(s for s in build_active_specs(config) if s.rule_id == "net_long_pct")
    expected: dict[str, float] = {}
    for rec in (rec_1, rec_2):
        proposal = translate_recommendation_to_proposed_delta(rec, snapshot=snap)
        dae = compute_delta_adjusted_exposure(proposal=proposal, market=market, config=config)
        expected[proposal.id] = net_long_spec.contribute(proposal, dae, snap, config)

    breaches_by_rule = {b.rule: b for b in result.breaches}
    breach = breaches_by_rule["net_long_pct"]
    actual = {c.proposal_id: c.contribution for c in breach.contributors}
    assert actual == expected
