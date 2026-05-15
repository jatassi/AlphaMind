"""Tests for run_proposal_pre_processor — ALP-318.

The runner is the public entry point that composes the prior waves' work
into a ``ProposalPreProcessorBundle``. Acceptance criteria from the story
map one-to-one to the test functions below.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, Literal

import jsonschema
import pytest

from alphamind._kernel.ids import (
    InvocationId,
    OrderId,
    PositionId,
    RecommendationId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.decision.analyst.models import (
    AnalystOutput,
    EntryOrder,
    GuardrailValidationResult,
    InstrumentEquity,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    Target,
    WatchlistEntry,
)
from alphamind.decision.proposal_pre_processor import (
    BUNDLE_OUTPUT_SCHEMA,
    BundleAssemblyError,
    ConflictType,
    ProposalPreProcessorBundle,
    run_proposal_pre_processor,
)
from alphamind.decision.proposal_pre_processor.models import AnalystSideConflict
from alphamind.decision.strategist.models import (
    AddParameters,
    CloseParameters,
    DefensivePostureSummary,
    ExposureImpact,
    ModificationParameters,
    PendingOrderAssessment,
    PortfolioLevelObservations,
    PositionAssessment,
    ReduceParameters,
    StrategistOutput,
)
from alphamind.decision.strategist.models import (
    EntryOrder as StratEntryOrder,
)
from alphamind.decision.strategist.models import (
    GuardrailValidationResult as StratGuardrailValidationResult,
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
)

# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_FINAL = datetime(2026, 4, 28, 14, 30, 5, tzinfo=UTC)
_EXP = date(2026, 5, 28)


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _library_config() -> LibraryConfig:
    """Full 13-rule config matching combined-set tests."""
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
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(
            options_enabled=True,
            short_selling_enabled=True,
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
        underlying_prices={underlying: 150.0, "NVDA": 150.0, "MSFT": 150.0},
        risk_free_rate=0.045,
        iv_provider=provider,
        as_of=_NOW,
    )


def _existing_long_equity(
    *,
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    notional_usd: float = 5_000.0,
    quantity: float = 33.0,
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
    existing_positions: dict[str, ExistingPosition] | None = None,
    sector_exposure_pct: dict[str, float] | None = None,
    net_long_pct: float = 5.0,
    gross_pct: float = 5.0,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {"tech": 5.0, "semis": 0.0, "financials": 0.0, "energy": 0.0}
    return PortfolioStateSnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(sector_exposure_pct),
        net_long_pct=net_long_pct,
        net_short_pct=0.0,
        gross_pct=gross_pct,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType(existing_positions or {}),
    )


def _guardrail_result_analyst() -> GuardrailValidationResult:
    return GuardrailValidationResult(overall="PASS", per_rule=(), checked_at=_NOW)


def _invalidation_leg(rec_id: str = "REC-1") -> InvalidationLeg:
    return InvalidationLeg(
        leg_id=f"INV-{rec_id[4:]}",
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
    direction: Literal["long", "short"] = "long",
    quantity: float = 20.0,
    dollar_value: float = 3_000.0,
    conviction_level: int = 3,
) -> Recommendation:
    leg = _invalidation_leg(rec_id)
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentEquity(
            asset_type="equity", ticker=Symbol(underlying), direction=direction
        ),
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        conviction_level=conviction_level,
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(
            quantity=quantity, dollar_value=money(dollar_value), pct_of_portfolio=3.0
        ),
        target=Target(
            target_type="absolute_price", price=price(200.0), dollar_pl_target=money(5000.0)
        ),
        invalidation_legs=(leg,),
        guardrail_validation_result=_guardrail_result_analyst(),
        thesis_narrative="Thesis prose.",
        target_rationale="Target prose.",
        invalidation_rationale=(InvalidationRationale(leg_id=leg.leg_id, rationale="Reason."),),
        position_size_rationale="Sizing prose.",
        counterarguments_acknowledged="None.",
        time_expectation_hours=24.0,
    )


def _watchlist_entry(ticker: str = "AAPL") -> WatchlistEntry:
    return WatchlistEntry(
        ticker=Symbol(ticker),
        sector="tech",
        thesis_summary="Watch for breakout.",
        estimated_conviction=3,
    )


def _hold_assessment(
    *,
    sa_id: str = "SA-1",
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    remedy_flag: str | None = None,
) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THESIS-{position_id[4:]}"),
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        thesis_status="on-track",
        recommended_action="hold",
        status_rationale="Thesis intact.",
        action_rationale="Hold for now.",
        remedy_flag=remedy_flag,
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
        thesis_id=ThesisId(f"THESIS-{position_id[4:]}"),
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        thesis_status="invalidated",
        recommended_action="close",
        action_parameters=CloseParameters(
            action="close",
            quantity="all",
            order_type="market",
            close_rationale_type="thesis_invalidated",
        ),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=signed_money(-5_000.0),
            net_directional_impact=signed_money(-5_000.0),
        ),
        status_rationale="Invalidated.",
        action_rationale="Close.",
    )


def _reduce_assessment(
    *,
    sa_id: str = "SA-1",
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THESIS-{position_id[4:]}"),
        underlying=Symbol(underlying),
        sector=sector,  # type: ignore[arg-type]
        thesis_status="partially-realized",
        recommended_action="reduce",
        action_parameters=ReduceParameters(action="reduce", quantity=10.0, order_type="market"),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=signed_money(-1_500.0),
            net_directional_impact=signed_money(-1_500.0),
        ),
        status_rationale="Partial target reached.",
        action_rationale="Lock in half.",
        reduce_rationale="Locking in half.",
    )


def _entry_pending_order(
    *,
    pending_id: str = "SA-ORD-1",
    linked_assessment_id: str | None = None,
    recommended_action: Literal["maintain", "modify", "cancel"] = "maintain",
) -> PendingOrderAssessment:
    modification = (
        ModificationParameters(new_limit_price=price(99.0))
        if recommended_action == "modify"
        else None
    )
    return PendingOrderAssessment(
        pending_order_assessment_id=RecommendationId(pending_id),
        order_id=OrderId(f"ORD-{pending_id[7:]}"),
        position_id=PositionId(f"POS-{pending_id[7:]}"),
        order_type="entry_limit",
        order_age_hours=2.0,
        fill_probability_assessment="plausible",
        recommended_action=recommended_action,
        modification_parameters=modification,
        linked_position_assessment_id=RecommendationId(linked_assessment_id)
        if linked_assessment_id is not None
        else None,
        drift_rationale="Drift.",
        action_rationale="Action.",
    )


def _portfolio_observations(
    *,
    defensive_posture_summary: DefensivePostureSummary | None = None,
) -> PortfolioLevelObservations:
    return PortfolioLevelObservations(
        aggregate_thesis_health="Healthy.",
        sector_balance_shifts="No shifts.",
        thesis_dependency_warnings="None.",
        capital_allocation_observations="Adequate.",
        defensive_posture_summary=defensive_posture_summary,
    )


def _defensive_summary() -> DefensivePostureSummary:
    return DefensivePostureSummary(
        reduction_priority=(),
        capital_preservation_notes="Holding cash; no add actions.",
    )


def _analyst_output(
    *,
    invocation_id: str = "inv-001",
    mode: Literal["normal", "watchlist"] = "normal",
    recommendations: tuple[Recommendation, ...] | None = None,
    watchlist: tuple[WatchlistEntry, ...] | None = None,
) -> AnalystOutput:
    if mode == "normal":
        return AnalystOutput(
            invocation_id=InvocationId(invocation_id),
            timestamp=_NOW,
            mode="normal",
            recommendations=recommendations if recommendations is not None else (),
        )
    return AnalystOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_NOW,
        mode="watchlist",
        watchlist=watchlist if watchlist is not None else (_watchlist_entry(),),
    )


def _strategist_output(
    *,
    invocation_id: str = "inv-001",
    mode: Literal["normal", "defensive_posture"] = "normal",
    position_assessments: tuple[PositionAssessment, ...] = (),
    pending_order_assessments: tuple[PendingOrderAssessment, ...] = (),
) -> StrategistOutput:
    obs = (
        _portfolio_observations(defensive_posture_summary=_defensive_summary())
        if mode == "defensive_posture"
        else _portfolio_observations()
    )
    return StrategistOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_NOW,
        mode=mode,
        position_assessments=position_assessments,
        pending_order_assessments=pending_order_assessments,
        portfolio_level_observations=obs,
    )


def _run(
    *,
    analyst_output: AnalystOutput | None = None,
    strategist_output: StrategistOutput | None = None,
    snapshot: PortfolioStateSnapshot | None = None,
) -> ProposalPreProcessorBundle:
    """Convenience wrapper around the runner with sensible defaults."""
    return run_proposal_pre_processor(
        analyst_output=analyst_output if analyst_output is not None else _analyst_output(),
        strategist_output=(
            strategist_output if strategist_output is not None else _strategist_output()
        ),
        snapshot=snapshot if snapshot is not None else _snapshot(),
        library_config=_library_config(),
        market=_market(),
        snapshot_timestamp=_NOW,
        timestamp=_FINAL,
    )


# ===========================================================================
# AC: Normal-mode bundle has correct basis IDs
# ===========================================================================


def test_normal_mode_basis_ids_track_input_order() -> None:
    """basis IDs reflect analyst REC ordering and strategist non-hold SA ordering."""
    rec_1 = _equity_recommendation(rec_id="REC-1", underlying=Symbol("AAPL"))
    rec_2 = _equity_recommendation(rec_id="REC-2", underlying=Symbol("MSFT"))
    close = _close_assessment(
        sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("NVDA")
    )
    hold = _hold_assessment(
        sa_id="SA-2", position_id=PositionId("POS-2"), underlying=Symbol("MSFT")
    )
    reduce_ = _reduce_assessment(
        sa_id="SA-3", position_id=PositionId("POS-3"), underlying=Symbol("AAPL")
    )

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("NVDA")),
        "POS-2": _existing_long_equity(position_id=PositionId("POS-2"), underlying=Symbol("MSFT")),
        "POS-3": _existing_long_equity(position_id=PositionId("POS-3"), underlying=Symbol("AAPL")),
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        analyst_output=_analyst_output(recommendations=(rec_1, rec_2)),
        strategist_output=_strategist_output(
            position_assessments=(close, hold, reduce_),
        ),
        snapshot=snap,
    )

    basis = bundle.aggregate_observations.combined_set_impact.basis
    assert basis.analyst_proposal_ids == ("REC-1", "REC-2")
    assert basis.strategist_action_ids == ("SA-1", "SA-3")
    assert basis.strategist_holds_excluded_count == 1


# ===========================================================================
# AC: Watchlist-mode bundle has correct shape
# ===========================================================================


def test_watchlist_mode_bundle_shape() -> None:
    """analyst.mode=watchlist + strategist.mode=defensive_posture → halt-mode bundle."""
    watchlist = (_watchlist_entry(ticker=Symbol("AAPL")), _watchlist_entry(ticker=Symbol("MSFT")))
    bundle = _run(
        analyst_output=_analyst_output(mode="watchlist", watchlist=watchlist),
        strategist_output=_strategist_output(mode="defensive_posture"),
    )

    assert bundle.analyst_section.mode == "watchlist"
    assert bundle.analyst_section.watchlist == watchlist
    assert bundle.analyst_section.recommendations is None
    assert bundle.strategist_section.mode == "defensive_posture"
    assert bundle.aggregate_observations.combined_set_impact.basis.analyst_proposal_ids == ()


# ===========================================================================
# AC: Disagreeing modes raise BundleAssemblyError
# ===========================================================================


def test_disagreeing_mode_analyst_watchlist_strategist_normal_raises() -> None:
    """analyst=watchlist + strategist=normal → BundleAssemblyError."""
    with pytest.raises(BundleAssemblyError, match="halt-state inconsistency"):
        _run(
            analyst_output=_analyst_output(mode="watchlist"),
            strategist_output=_strategist_output(mode="normal"),
        )


def test_disagreeing_mode_analyst_normal_strategist_defensive_raises() -> None:
    """analyst=normal + strategist=defensive_posture → BundleAssemblyError."""
    with pytest.raises(BundleAssemblyError, match="halt-state inconsistency"):
        _run(
            analyst_output=_analyst_output(mode="normal"),
            strategist_output=_strategist_output(mode="defensive_posture"),
        )


# ===========================================================================
# AC: Disagreeing invocation_id raises BundleAssemblyError
# ===========================================================================


def test_disagreeing_invocation_ids_raise() -> None:
    """analyst.invocation_id != strategist.invocation_id → BundleAssemblyError."""
    with pytest.raises(BundleAssemblyError, match="invocation_id mismatch"):
        _run(
            analyst_output=_analyst_output(invocation_id="inv-A"),
            strategist_output=_strategist_output(invocation_id="inv-B"),
        )


# ===========================================================================
# AC: §2 position_assessments order matches strategist order
# ===========================================================================


def test_position_assessment_order_preserved() -> None:
    """Wrapped position_assessments order matches strategist_output.position_assessments order."""
    sa1 = _close_assessment(
        sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("AAPL")
    )
    sa2 = _hold_assessment(sa_id="SA-2", position_id=PositionId("POS-2"), underlying=Symbol("MSFT"))
    sa3 = _reduce_assessment(
        sa_id="SA-3", position_id=PositionId("POS-3"), underlying=Symbol("NVDA")
    )

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL")),
        "POS-2": _existing_long_equity(position_id=PositionId("POS-2"), underlying=Symbol("MSFT")),
        "POS-3": _existing_long_equity(position_id=PositionId("POS-3"), underlying=Symbol("NVDA")),
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        strategist_output=_strategist_output(position_assessments=(sa1, sa2, sa3)),
        snapshot=snap,
    )

    section_ids = tuple(
        wp.assessment.assessment_id for wp in bundle.strategist_section.position_assessments
    )
    assert section_ids == ("SA-1", "SA-2", "SA-3")


# ===========================================================================
# AC: §2 pending_order_assessments order matches strategist order
# ===========================================================================


def test_pending_order_assessment_order_preserved() -> None:
    """Wrapped pending_order_assessments order matches the strategist's emitted order."""
    sa = _hold_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    p1 = _entry_pending_order(pending_id="SA-ORD-1", linked_assessment_id="SA-1")
    p2 = _entry_pending_order(
        pending_id="SA-ORD-2", linked_assessment_id="SA-1", recommended_action="modify"
    )
    p3 = _entry_pending_order(
        pending_id="SA-ORD-3", linked_assessment_id="SA-1", recommended_action="cancel"
    )

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        strategist_output=_strategist_output(
            position_assessments=(sa,),
            pending_order_assessments=(p1, p2, p3),
        ),
        snapshot=snap,
    )

    pending_ids = tuple(
        wp.pending_order_assessment.pending_order_assessment_id
        for wp in bundle.strategist_section.pending_order_assessments
    )
    assert pending_ids == ("SA-ORD-1", "SA-ORD-2", "SA-ORD-3")


# ===========================================================================
# AC: §3 recommendations order matches analyst order
# ===========================================================================


def test_recommendation_order_preserved() -> None:
    """Wrapped recommendations order matches analyst_output.recommendations order."""
    r1 = _equity_recommendation(rec_id="REC-1", underlying=Symbol("AAPL"))
    r2 = _equity_recommendation(rec_id="REC-2", underlying=Symbol("MSFT"))
    r3 = _equity_recommendation(rec_id="REC-3", underlying=Symbol("NVDA"))

    bundle = _run(analyst_output=_analyst_output(recommendations=(r1, r2, r3)))

    assert bundle.analyst_section.recommendations is not None
    rec_ids = tuple(
        wr.recommendation.recommendation_id for wr in bundle.analyst_section.recommendations
    )
    assert rec_ids == ("REC-1", "REC-2", "REC-3")


# ===========================================================================
# AC: Inner records preserved byte-for-byte
# ===========================================================================


def test_inner_records_preserved_byte_for_byte() -> None:
    """Each wrapped record's inner ``model_dump()`` slice equals the source record's dump.

    The wrapper only adds the pre_processor_annotations field; the inner record's
    serialization must be unchanged.
    """
    rec = _equity_recommendation(rec_id="REC-1", underlying=Symbol("AAPL"))
    sa = _close_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    pending = _entry_pending_order(pending_id="SA-ORD-1", linked_assessment_id="SA-1")

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        analyst_output=_analyst_output(recommendations=(rec,)),
        strategist_output=_strategist_output(
            position_assessments=(sa,),
            pending_order_assessments=(pending,),
        ),
        snapshot=snap,
    )

    # Position assessment inner-record dump must equal source dump
    wrapped_sa = bundle.strategist_section.position_assessments[0]
    assert wrapped_sa.assessment.model_dump() == sa.model_dump()

    # Pending-order assessment inner-record dump must equal source dump
    wrapped_pending = bundle.strategist_section.pending_order_assessments[0]
    assert wrapped_pending.pending_order_assessment.model_dump() == pending.model_dump()

    # Recommendation inner-record dump must equal source dump
    assert bundle.analyst_section.recommendations is not None
    wrapped_rec = bundle.analyst_section.recommendations[0]
    assert wrapped_rec.recommendation.model_dump() == rec.model_dump()


# ===========================================================================
# AC: Mirror-symmetry property holds end-to-end
# ===========================================================================


def test_mirror_symmetry_end_to_end() -> None:
    """Every analyst-side conflict has a matching strategist-side mirror.

    Builds a normal-mode bundle with multiple conflict types (entry_vs_close,
    entry_vs_hold, entry_vs_pending_maintain) and asserts mirror-symmetry by
    walking the bundle.
    """
    # Two analyst entries both targeting NVDA (one matching close, one matching pending).
    rec_close = _equity_recommendation(rec_id="REC-1", underlying=Symbol("NVDA"), direction="long")
    rec_pending = _equity_recommendation(
        rec_id="REC-2", underlying=Symbol("MSFT"), direction="long"
    )
    rec_hold = _equity_recommendation(rec_id="REC-3", underlying=Symbol("AAPL"), direction="long")

    sa_close = _close_assessment(
        sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("NVDA")
    )
    sa_hold = _hold_assessment(
        sa_id="SA-2", position_id=PositionId("POS-2"), underlying=Symbol("AAPL")
    )
    sa_link = _hold_assessment(
        sa_id="SA-3", position_id=PositionId("POS-3"), underlying=Symbol("MSFT")
    )

    p1 = _entry_pending_order(
        pending_id="SA-ORD-1",
        linked_assessment_id="SA-3",
        recommended_action="maintain",
    )

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("NVDA")),
        "POS-2": _existing_long_equity(position_id=PositionId("POS-2"), underlying=Symbol("AAPL")),
        "POS-3": _existing_long_equity(position_id=PositionId("POS-3"), underlying=Symbol("MSFT")),
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        analyst_output=_analyst_output(recommendations=(rec_close, rec_pending, rec_hold)),
        strategist_output=_strategist_output(
            position_assessments=(sa_close, sa_hold, sa_link),
            pending_order_assessments=(p1,),
        ),
        snapshot=snap,
    )

    assert bundle.analyst_section.recommendations is not None
    # Build lookup tables for the strategist side
    pos_conflicts: dict[str, tuple[Any, ...]] = {
        wp.assessment.assessment_id: wp.pre_processor_annotations.conflicts
        for wp in bundle.strategist_section.position_assessments
    }
    pending_conflicts: dict[str, tuple[Any, ...]] = {
        wp.pending_order_assessment.pending_order_assessment_id: (
            wp.pre_processor_annotations.conflicts
        )
        for wp in bundle.strategist_section.pending_order_assessments
    }

    # For each analyst-side conflict, find the mirror entry on the strategist side
    saw_position_match = False
    saw_pending_match = False
    for wrapped_rec in bundle.analyst_section.recommendations:
        for analyst_conflict in wrapped_rec.pre_processor_annotations.conflicts:
            if analyst_conflict.with_assessment_id is not None:
                strat_side = pos_conflicts[analyst_conflict.with_assessment_id]
                matches = [
                    c
                    for c in strat_side
                    if c.with_recommendation_id == wrapped_rec.recommendation.recommendation_id
                    and c.underlying == analyst_conflict.underlying
                    and c.conflict_type == analyst_conflict.conflict_type
                ]
                assert matches, (
                    f"no mirror entry for analyst-side conflict on rec="
                    f"{wrapped_rec.recommendation.recommendation_id} → "
                    f"{analyst_conflict.with_assessment_id}"
                )
                saw_position_match = True
            elif analyst_conflict.with_pending_order_assessment_id is not None:
                strat_side = pending_conflicts[analyst_conflict.with_pending_order_assessment_id]
                matches = [
                    c
                    for c in strat_side
                    if c.with_recommendation_id == wrapped_rec.recommendation.recommendation_id
                    and c.underlying == analyst_conflict.underlying
                    and c.conflict_type == analyst_conflict.conflict_type
                ]
                assert matches, (
                    f"no mirror entry for analyst-side conflict on rec="
                    f"{wrapped_rec.recommendation.recommendation_id} → "
                    f"{analyst_conflict.with_pending_order_assessment_id}"
                )
                saw_pending_match = True

    assert saw_position_match, "expected at least one position-side mirror"
    assert saw_pending_match, "expected at least one pending-side mirror"


# ===========================================================================
# AC: Mirror-symmetry — strategist→analyst direction
# ===========================================================================


def test_mirror_symmetry_strategist_side_to_analyst() -> None:
    """Every strategist-side conflict has a matching analyst-side entry on the recommendation."""
    rec = _equity_recommendation(rec_id="REC-1", underlying=Symbol("NVDA"), direction="long")
    sa = _close_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("NVDA"))
    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("NVDA"))
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        analyst_output=_analyst_output(recommendations=(rec,)),
        strategist_output=_strategist_output(position_assessments=(sa,)),
        snapshot=snap,
    )

    assert bundle.analyst_section.recommendations is not None
    analyst_conflicts_by_rec_id: dict[str, tuple[AnalystSideConflict, ...]] = {
        wr.recommendation.recommendation_id: wr.pre_processor_annotations.conflicts
        for wr in bundle.analyst_section.recommendations
    }
    for wrapped_sa in bundle.strategist_section.position_assessments:
        for sc in wrapped_sa.pre_processor_annotations.conflicts:
            mirrored = analyst_conflicts_by_rec_id[sc.with_recommendation_id]
            matches = [
                ac
                for ac in mirrored
                if ac.with_assessment_id == wrapped_sa.assessment.assessment_id
                and ac.underlying == sc.underlying
                and ac.conflict_type == sc.conflict_type
            ]
            assert matches, (
                f"no mirror entry for strategist-side conflict on assessment="
                f"{wrapped_sa.assessment.assessment_id}"
            )


# ===========================================================================
# AC: strategist_holds_excluded_count equals count of hold actions
# ===========================================================================


def test_strategist_holds_excluded_count_matches_hold_assessments() -> None:
    """basis.strategist_holds_excluded_count equals the count of hold assessments."""
    sa1 = _hold_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    sa2 = _hold_assessment(sa_id="SA-2", position_id=PositionId("POS-2"), underlying=Symbol("MSFT"))
    sa3 = _close_assessment(
        sa_id="SA-3", position_id=PositionId("POS-3"), underlying=Symbol("NVDA")
    )
    sa4 = _hold_assessment(sa_id="SA-4", position_id=PositionId("POS-4"), underlying=Symbol("AAPL"))

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL")),
        "POS-2": _existing_long_equity(position_id=PositionId("POS-2"), underlying=Symbol("MSFT")),
        "POS-3": _existing_long_equity(position_id=PositionId("POS-3"), underlying=Symbol("NVDA")),
        "POS-4": _existing_long_equity(position_id=PositionId("POS-4"), underlying=Symbol("AAPL")),
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        strategist_output=_strategist_output(position_assessments=(sa1, sa2, sa3, sa4)),
        snapshot=snap,
    )

    basis = bundle.aggregate_observations.combined_set_impact.basis
    assert basis.strategist_holds_excluded_count == 3


# ===========================================================================
# AC: conviction_distribution.total equals number of recommendations
# ===========================================================================


def test_conviction_distribution_total_matches_recommendation_count() -> None:
    """conviction_distribution.total == len(analyst_section.recommendations)."""
    recs = (
        _equity_recommendation(rec_id="REC-1", conviction_level=2),
        _equity_recommendation(rec_id="REC-2", conviction_level=4),
        _equity_recommendation(rec_id="REC-3", conviction_level=4),
    )
    bundle = _run(analyst_output=_analyst_output(recommendations=recs))

    assert bundle.aggregate_observations.conviction_distribution.total == 3
    assert bundle.analyst_section.recommendations is not None
    assert len(bundle.analyst_section.recommendations) == 3


def test_conviction_distribution_total_zero_in_watchlist_mode() -> None:
    """Watchlist mode: conviction_distribution.total == 0 (no recommendations)."""
    bundle = _run(
        analyst_output=_analyst_output(mode="watchlist"),
        strategist_output=_strategist_output(mode="defensive_posture"),
    )
    assert bundle.aggregate_observations.conviction_distribution.total == 0
    assert bundle.analyst_section.recommendations is None


# ===========================================================================
# AC: book_health_summary.total equals number of position assessments
# ===========================================================================


def test_book_health_summary_total_matches_position_count() -> None:
    """book_health_summary.total == len(strategist_section.position_assessments)."""
    sa1 = _hold_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    sa2 = _close_assessment(
        sa_id="SA-2", position_id=PositionId("POS-2"), underlying=Symbol("MSFT")
    )

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL")),
        "POS-2": _existing_long_equity(position_id=PositionId("POS-2"), underlying=Symbol("MSFT")),
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        strategist_output=_strategist_output(position_assessments=(sa1, sa2)),
        snapshot=snap,
    )

    assert bundle.aggregate_observations.book_health_summary.total == 2
    assert len(bundle.strategist_section.position_assessments) == 2


# ===========================================================================
# AC: Bundle validates against BUNDLE_OUTPUT_SCHEMA
# ===========================================================================


def test_bundle_dump_validates_against_schema() -> None:
    """dataclasses.asdict(ProposalPreProcessorBundle) validates against BUNDLE_OUTPUT_SCHEMA."""
    rec = _equity_recommendation(rec_id="REC-1", underlying=Symbol("AAPL"))
    sa = _close_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    pending = _entry_pending_order(pending_id="SA-ORD-1", linked_assessment_id="SA-1")

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        analyst_output=_analyst_output(recommendations=(rec,)),
        strategist_output=_strategist_output(
            position_assessments=(sa,),
            pending_order_assessments=(pending,),
        ),
        snapshot=snap,
    )

    payload = bundle.model_dump(mode="json", by_alias=True)
    jsonschema.validate(instance=payload, schema=BUNDLE_OUTPUT_SCHEMA)


def test_watchlist_bundle_dump_validates_against_schema() -> None:
    """Watchlist-mode bundle validates against schema."""
    bundle = _run(
        analyst_output=_analyst_output(mode="watchlist"),
        strategist_output=_strategist_output(mode="defensive_posture"),
    )
    payload = bundle.model_dump(mode="json", by_alias=True)
    jsonschema.validate(instance=payload, schema=BUNDLE_OUTPUT_SCHEMA)


# ===========================================================================
# AC: Runner is pure — equal inputs produce equal outputs
# ===========================================================================


def test_runner_is_pure_two_calls_produce_equal_bundles() -> None:
    """Calling the runner twice with the same inputs produces equal bundles."""
    rec = _equity_recommendation(rec_id="REC-1", underlying=Symbol("AAPL"))
    sa = _close_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    }
    snap = _snapshot(existing_positions=existing)

    bundle_a = _run(
        analyst_output=_analyst_output(recommendations=(rec,)),
        strategist_output=_strategist_output(position_assessments=(sa,)),
        snapshot=snap,
    )
    bundle_b = _run(
        analyst_output=_analyst_output(recommendations=(rec,)),
        strategist_output=_strategist_output(position_assessments=(sa,)),
        snapshot=snap,
    )
    assert bundle_a == bundle_b


# ===========================================================================
# AC: No claude_agent_sdk import; no clock read; no uuid generation
# ===========================================================================


def test_runner_module_does_not_import_sdk_or_uuid() -> None:
    """Static check: the runner and assembler modules import nothing forbidden.

    Reading source guards against accidental drift; ``run_proposal_pre_processor``
    must remain pure.
    """
    import inspect

    from alphamind.decision.proposal_pre_processor import assembler, runner

    for module in (runner, assembler):
        source = inspect.getsource(module)
        assert "claude_agent_sdk" not in source, (
            f"{module.__name__} must not import claude_agent_sdk"
        )
        assert "uuid.uuid4" not in source, f"{module.__name__} must not call uuid.uuid4()"
        assert "datetime.now" not in source, f"{module.__name__} must not read the clock"
        assert "datetime.utcnow" not in source, f"{module.__name__} must not read the clock"


# ===========================================================================
# AC: Bundle invocation_id matches both analyst and strategist when consistent
# ===========================================================================


def test_bundle_invocation_id_copied_from_inputs() -> None:
    """invocation_id is copied forward from the analyst (asserts ==strategist)."""
    bundle = _run(
        analyst_output=_analyst_output(invocation_id="inv-XYZ"),
        strategist_output=_strategist_output(invocation_id="inv-XYZ"),
    )
    assert bundle.invocation_id == "inv-XYZ"


# ===========================================================================
# AC: Pending entry orders without a linked assessment carry no conflicts
# ===========================================================================


def test_pending_without_linked_assessment_carries_empty_conflicts() -> None:
    """Per ALP-316 contract: pending entry orders without a resolvable link yield no conflicts.

    The runner does not work around this; it forwards inputs naturally.
    """
    rec = _equity_recommendation(rec_id="REC-1", underlying=Symbol("AAPL"))
    sa = _hold_assessment(sa_id="SA-1", position_id=PositionId("POS-1"), underlying=Symbol("MSFT"))
    pending_no_link = _entry_pending_order(pending_id="SA-ORD-1", linked_assessment_id=None)

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("MSFT"))
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        analyst_output=_analyst_output(recommendations=(rec,)),
        strategist_output=_strategist_output(
            position_assessments=(sa,),
            pending_order_assessments=(pending_no_link,),
        ),
        snapshot=snap,
    )
    wrapped_pending = bundle.strategist_section.pending_order_assessments[0]
    assert wrapped_pending.pre_processor_annotations.conflicts == ()


# ===========================================================================
# AC: AddParameters analyst conflict produces entry_vs_add classification
# ===========================================================================


def test_add_action_yields_entry_vs_add_conflict() -> None:
    """A strategist add on the same underlying as an analyst entry → entry_vs_add."""
    rec = _equity_recommendation(rec_id="REC-1", underlying=Symbol("AAPL"), direction="long")

    add_assessment = PositionAssessment(
        assessment_id=RecommendationId("SA-1"),
        position_id=PositionId("POS-1"),
        thesis_id=ThesisId("THESIS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        thesis_status="on-track",
        recommended_action="add",
        action_parameters=AddParameters(
            action="add",
            additional_quantity=10.0,
            additional_dollar_value=money(1500.0),
            entry_order=StratEntryOrder(type="market"),
        ),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=money(1500.0), net_directional_impact=money(1500.0)
        ),
        guardrail_validation_result=StratGuardrailValidationResult(
            overall="PASS", per_rule=(), checked_at=_NOW
        ),
        add_conviction_justification="conviction increased",
        status_rationale="Thesis intact.",
        action_rationale="Add.",
    )

    existing = {
        "POS-1": _existing_long_equity(position_id=PositionId("POS-1"), underlying=Symbol("AAPL"))
    }
    snap = _snapshot(existing_positions=existing)

    bundle = _run(
        analyst_output=_analyst_output(recommendations=(rec,)),
        strategist_output=_strategist_output(position_assessments=(add_assessment,)),
        snapshot=snap,
    )
    assert bundle.analyst_section.recommendations is not None
    conflicts = bundle.analyst_section.recommendations[0].pre_processor_annotations.conflicts
    assert any(c.conflict_type == ConflictType.entry_vs_add for c in conflicts)
