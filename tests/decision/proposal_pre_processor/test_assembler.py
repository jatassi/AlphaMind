"""Tests for proposal_pre_processor/assembler.py — ALP-318.

Focused unit tests for the assembler's helper functions. The full runner
end-to-end behavior is tested in test_runner.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

import pytest

from alphamind._kernel.ids import (
    InvocationId,
    PositionId,
    RecommendationId,
    Symbol,
    ThesisId,
)
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
from alphamind.decision.proposal_pre_processor.assembler import (
    BundleAssemblyError,
    build_analyst_section,
    build_held_direction_resolver,
    build_strategist_section,
    verify_halt_state_consistency,
    verify_invocation_id_consistency,
)
from alphamind.decision.proposal_pre_processor.conflicts import (
    ConflictDetectionResult,
)
from alphamind.decision.proposal_pre_processor.models import (
    AnalystSideConflict,
    ConflictType,
    StrategistSideConflict,
)
from alphamind.decision.strategist.models import (
    CloseParameters,
    DefensivePostureSummary,
    ExposureImpact,
    PendingOrderAssessment,
    PortfolioLevelObservations,
    PositionAssessment,
    StrategistOutput,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    AssetType,
    Direction,
    ExistingPosition,
    PortfolioStateSnapshot,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _existing(
    *,
    position_id: str,
    underlying: str,
    direction: Direction,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=position_id,
        underlying=underlying,
        sector="tech",
        direction=direction,
        asset_type=AssetType.EQUITY,
        notional_usd=1_000.0,
        delta_adjusted_exposure_usd=1_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
        quantity=10.0,
    )


def _snapshot_with_positions(positions: dict[str, ExistingPosition]) -> PortfolioStateSnapshot:
    return PortfolioStateSnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=50_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": 0.0}),
        net_long_pct=0.0,
        net_short_pct=0.0,
        gross_pct=0.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=0.0,
        existing_positions=MappingProxyType(positions),
    )


def _portfolio_observations(*, defensive: bool = False) -> PortfolioLevelObservations:
    summary = (
        DefensivePostureSummary(
            reduction_priority=(),
            capital_preservation_notes="Hold cash.",
        )
        if defensive
        else None
    )
    return PortfolioLevelObservations(
        aggregate_thesis_health="Healthy.",
        sector_balance_shifts="No shifts.",
        thesis_dependency_warnings="None.",
        capital_allocation_observations="Adequate.",
        defensive_posture_summary=summary,
    )


def _analyst_output_normal(
    *,
    invocation_id: str = "inv-1",
    recommendations: tuple[Recommendation, ...] = (),
) -> AnalystOutput:
    return AnalystOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_NOW,
        mode="normal",
        recommendations=recommendations,
    )


def _analyst_output_watchlist(
    *,
    invocation_id: str = "inv-1",
    watchlist: tuple[WatchlistEntry, ...] = (),
) -> AnalystOutput:
    return AnalystOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_NOW,
        mode="watchlist",
        watchlist=watchlist
        or (
            WatchlistEntry(
                ticker=Symbol("AAPL"),
                sector="tech",
                thesis_summary="watch.",
                estimated_conviction=3,
            ),
        ),
    )


def _strategist_output(
    *,
    invocation_id: str = "inv-1",
    mode: Literal["normal", "defensive_posture"] = "normal",
    position_assessments: tuple[PositionAssessment, ...] = (),
    pending_order_assessments: tuple[PendingOrderAssessment, ...] = (),
) -> StrategistOutput:
    return StrategistOutput(
        invocation_id=InvocationId(invocation_id),
        timestamp=_NOW,
        mode=mode,
        position_assessments=position_assessments,
        pending_order_assessments=pending_order_assessments,
        portfolio_level_observations=_portfolio_observations(
            defensive=(mode == "defensive_posture")
        ),
    )


def _equity_recommendation(rec_id: str = "REC-1", underlying: str = "NVDA") -> Recommendation:
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentEquity(
            asset_type="equity", ticker=Symbol(underlying), direction="long"
        ),
        underlying=Symbol(underlying),
        sector="tech",
        conviction_level=3,
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=10.0, dollar_value=1000.0, pct_of_portfolio=1.0),
        target=Target(target_type="absolute_price", price=200.0, dollar_pl_target=500.0),
        invalidation_legs=(
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=Symbol(underlying), comparator="<=", trigger_price=90.0
                ),
                order_parameters=OrderParameters(order_type="market"),
            ),
        ),
        time_expectation_hours=24.0,
        guardrail_validation_result=GuardrailValidationResult(
            overall="PASS", per_rule=(), checked_at=_NOW
        ),
        thesis_narrative="thesis",
        target_rationale="target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="reason"),),
        position_size_rationale="size",
        counterarguments_acknowledged="ack",
    )


def _close_assessment(
    sa_id: str = "SA-1", position_id: str = "POS-1", underlying: str = "NVDA"
) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(sa_id),
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THESIS-{position_id[4:]}"),
        underlying=Symbol(underlying),
        sector="tech",
        thesis_status="invalidated",
        recommended_action="close",
        action_parameters=CloseParameters(
            action="close",
            quantity="all",
            order_type="market",
            close_rationale_type="thesis_invalidated",
        ),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
        ),
        status_rationale="invalidated",
        action_rationale="closing",
    )


def _empty_conflicts() -> ConflictDetectionResult:
    return ConflictDetectionResult(
        analyst_conflicts_by_rec_id={},
        strategist_position_conflicts_by_assessment_id={},
        strategist_pending_conflicts_by_assessment_id={},
    )


# ===========================================================================
# verify_halt_state_consistency
# ===========================================================================


def test_halt_state_consistent_normal_pair_passes() -> None:
    verify_halt_state_consistency(
        analyst_output=_analyst_output_normal(),
        strategist_output=_strategist_output(mode="normal"),
    )


def test_halt_state_consistent_halt_pair_passes() -> None:
    verify_halt_state_consistency(
        analyst_output=_analyst_output_watchlist(),
        strategist_output=_strategist_output(mode="defensive_posture"),
    )


def test_halt_state_analyst_watchlist_strategist_normal_raises() -> None:
    with pytest.raises(BundleAssemblyError, match="halt-state inconsistency"):
        verify_halt_state_consistency(
            analyst_output=_analyst_output_watchlist(),
            strategist_output=_strategist_output(mode="normal"),
        )


def test_halt_state_analyst_normal_strategist_defensive_raises() -> None:
    with pytest.raises(BundleAssemblyError, match="halt-state inconsistency"):
        verify_halt_state_consistency(
            analyst_output=_analyst_output_normal(),
            strategist_output=_strategist_output(mode="defensive_posture"),
        )


# ===========================================================================
# verify_invocation_id_consistency
# ===========================================================================


def test_invocation_id_match_passes() -> None:
    verify_invocation_id_consistency(
        analyst_output=_analyst_output_normal(invocation_id="inv-A"),
        strategist_output=_strategist_output(invocation_id="inv-A"),
    )


def test_invocation_id_mismatch_raises() -> None:
    with pytest.raises(BundleAssemblyError, match="invocation_id mismatch"):
        verify_invocation_id_consistency(
            analyst_output=_analyst_output_normal(invocation_id="inv-A"),
            strategist_output=_strategist_output(invocation_id="inv-B"),
        )


# ===========================================================================
# build_held_direction_resolver
# ===========================================================================


def test_held_direction_resolver_resolves_long() -> None:
    snap = _snapshot_with_positions(
        {"POS-LONG": _existing(position_id="POS-LONG", underlying="AAPL", direction=Direction.LONG)}
    )
    resolver = build_held_direction_resolver(snap)
    assert resolver("POS-LONG") == "long"


def test_held_direction_resolver_resolves_short() -> None:
    snap = _snapshot_with_positions(
        {"POS-S": _existing(position_id="POS-S", underlying="AAPL", direction=Direction.SHORT)}
    )
    resolver = build_held_direction_resolver(snap)
    assert resolver("POS-S") == "short"


# ===========================================================================
# build_strategist_section
# ===========================================================================


def test_strategist_section_preserves_order_and_passes_through_observations() -> None:
    sa1 = _close_assessment(sa_id="SA-1", position_id="POS-1", underlying="AAPL")
    sa2 = _close_assessment(sa_id="SA-2", position_id="POS-2", underlying="MSFT")
    output = _strategist_output(position_assessments=(sa1, sa2))

    section = build_strategist_section(output, _empty_conflicts())

    assert section.mode == "normal"
    assert tuple(w.assessment.assessment_id for w in section.position_assessments) == (
        "SA-1",
        "SA-2",
    )
    # portfolio_level_observations passed through verbatim.
    assert section.portfolio_level_observations == output.portfolio_level_observations


def test_strategist_section_threads_provided_conflicts() -> None:
    sa = _close_assessment()
    output = _strategist_output(position_assessments=(sa,))

    conflict = StrategistSideConflict(
        with_recommendation_id="REC-7",
        underlying="NVDA",
        conflict_type=ConflictType.entry_vs_close,
    )
    conflicts = ConflictDetectionResult(
        analyst_conflicts_by_rec_id={},
        strategist_position_conflicts_by_assessment_id={"SA-1": (conflict,)},
        strategist_pending_conflicts_by_assessment_id={},
    )

    section = build_strategist_section(output, conflicts)
    assert section.position_assessments[0].pre_processor_annotations.conflicts == (conflict,)


# ===========================================================================
# build_analyst_section
# ===========================================================================


def test_analyst_section_normal_wraps_recommendations_in_order() -> None:
    r1 = _equity_recommendation("REC-1", "AAPL")
    r2 = _equity_recommendation("REC-2", "MSFT")
    output = _analyst_output_normal(recommendations=(r1, r2))

    section = build_analyst_section(output, _empty_conflicts())

    assert section.mode == "normal"
    assert section.watchlist is None
    assert section.recommendations is not None
    assert tuple(w.recommendation.recommendation_id for w in section.recommendations) == (
        "REC-1",
        "REC-2",
    )


def test_analyst_section_normal_threads_conflicts() -> None:
    rec = _equity_recommendation()
    output = _analyst_output_normal(recommendations=(rec,))

    conflict = AnalystSideConflict(
        with_assessment_id="SA-9",
        underlying="NVDA",
        conflict_type=ConflictType.entry_vs_close,
    )
    conflicts = ConflictDetectionResult(
        analyst_conflicts_by_rec_id={"REC-1": (conflict,)},
        strategist_position_conflicts_by_assessment_id={},
        strategist_pending_conflicts_by_assessment_id={},
    )

    section = build_analyst_section(output, conflicts)
    assert section.recommendations is not None
    assert section.recommendations[0].pre_processor_annotations.conflicts == (conflict,)


def test_analyst_section_watchlist_passes_watchlist_through() -> None:
    entry = WatchlistEntry(
        ticker=Symbol("AAPL"),
        sector="tech",
        thesis_summary="watch.",
        estimated_conviction=3,
    )
    output = _analyst_output_watchlist(watchlist=(entry,))

    section = build_analyst_section(output, _empty_conflicts())

    assert section.mode == "watchlist"
    assert section.recommendations is None
    assert section.watchlist == (entry,)


def test_analyst_section_watchlist_ignores_conflicts() -> None:
    """Watchlist mode never wraps records, so any conflicts arg is moot."""
    entry = WatchlistEntry(
        ticker=Symbol("AAPL"),
        sector="tech",
        thesis_summary="watch.",
        estimated_conviction=3,
    )
    output = _analyst_output_watchlist(watchlist=(entry,))

    # Even with a non-empty conflicts arg, watchlist mode produces no wrapped recs.
    conflicts: dict[str, Any] = {
        "REC-1": (
            AnalystSideConflict(
                with_assessment_id="SA-1",
                underlying="AAPL",
                conflict_type=ConflictType.entry_vs_close,
            ),
        )
    }
    result = ConflictDetectionResult(
        analyst_conflicts_by_rec_id=conflicts,
        strategist_position_conflicts_by_assessment_id={},
        strategist_pending_conflicts_by_assessment_id={},
    )

    section = build_analyst_section(output, result)
    assert section.recommendations is None
    assert section.watchlist == (entry,)
