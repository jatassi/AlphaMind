"""Tests for consumers/portfolio_manager.py — story 07."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import price, signed_money
from alphamind.portfolio_state.aggregates.thesis_quality import (
    AlphaBetaDecomposition,
    AttributionDimension,
    ConvictionCalibrationEntry,
    ConvictionSizingDeviation,
    InvalidationTimingClass,
    InvalidationTimingStat,
    PerformanceAttributionEntry,
    ResolutionWindowCounts,
    SignalHitRate,
    SignalToThesisConversion,
    ThesisDurationStat,
    ThesisQualityAggregate,
    TrailingWindow,
)
from alphamind.portfolio_state.consumers.portfolio_manager import (
    PortfolioManagerThesisComponentReader,
    SnapshotBackedThesisComponentReader,
    project_portfolio_manager_view,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
    PositionOpenedDetail,
    PositionOpenMechanism,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
    SectorExposureEntry,
)
from tests.portfolio_state.consumers._builders import (
    _make_active_risk_params,
    _make_bracket,
    _make_cash_ledger,
    _make_drawdown_state,
    _make_open_position,
    _make_pending_order,
    _make_risk_budget,
)

# ---------------------------------------------------------------------------
# Shared timestamps / IDs
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2025, 1, 1, 10, 1, 0, tzinfo=UTC)
_T2 = datetime(2025, 1, 1, 10, 2, 0, tzinfo=UTC)
_INV_ID = "inv-pm-001"


# ---------------------------------------------------------------------------
# Low-level record builders
# ---------------------------------------------------------------------------


def _make_thesis(
    thesis_id: str = "THESIS-001",
    position_id: str = "POS-001",
) -> ThesisRecord:
    def _comp(ctype: ThesisComponentType, cid: str) -> ThesisComponent:
        return ThesisComponent(
            component_id=cid,
            thesis_id=ThesisId(thesis_id),
            component_type=ctype,
            linked_bracket_leg_type=None,
            instrument_reference="AAPL",
            narrative="Narrative text.",
            key_assumptions=(KeyAssumption(text="Assumption", outcome=None),),
            generation_timestamp=_T0,
            resolution_outcome=None,
            resolution_notes=None,
        )

    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="Long AAPL on momentum.",
        components=(
            _comp(ThesisComponentType.ENTRY_RATIONALE, "comp-1"),
            _comp(ThesisComponentType.TARGET_RATIONALE, "comp-2"),
            _comp(ThesisComponentType.INVALIDATION_RATIONALE, "comp-3"),
        ),
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=_T0,
        time_expectation_hours=24.0,
        age_hours=4.0,
        expected_resolution_at=_T0 + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
        key_catalyst="earnings beat",
    )


def _make_pm_decision_entry(entry_id: str = "ENTRY-PM-001") -> ActivityLogEntry:
    detail = PMDecisionDetail(
        envelope_id="env-001",
        source_provenance_json={},
        evaluation_json={},
        modifications_json=[],
        resulting_command_ids=(),
        verdict=PMVerdict.APPROVE,
        originating_proposal_json={},
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INV_ID,
        timestamp=_T0,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def _make_changelog_entry(
    entry_id: str = "ENTRY-CL-001",
    position_id: str | None = "POS-001",
) -> ActivityLogEntry:
    detail = PositionOpenedDetail(
        ticker=Symbol("AAPL"),
        direction="LONG",
        fill_price=price("150.0"),
        quantity=100.0,
        thesis_id=None,
        bracket_id=None,
        mechanism=PositionOpenMechanism.ORDER_FILL,
        parent_position_id=None,
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INV_ID,
        timestamp=_T0,
        event_type=EventType.POSITION_OPENED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.FILL_PROCESSOR,
        detail=detail,
    )


def _make_thesis_quality() -> ThesisQualityAggregate:
    rwc = ResolutionWindowCounts(
        window=TrailingWindow.FIVE_DAYS,
        total_resolutions=0,
        validated=0,
        profitable_but_wrong=0,
        invalidated_stopped_correctly=0,
        invalidated_wrong_on_exit=0,
        cancelled_never_entered=0,
    )
    dur = ThesisDurationStat(
        window=TrailingWindow.FIVE_DAYS,
        mean_actual_to_expected_ratio=1.0,
        median_actual_to_expected_ratio=1.0,
        count=0,
    )
    inv_timing = InvalidationTimingStat(
        window=TrailingWindow.FIVE_DAYS,
        class_distribution={
            InvalidationTimingClass.EARLY: 0,
            InvalidationTimingClass.ON_TIME: 0,
            InvalidationTimingClass.LATE: 0,
        },
        mean_position_age_at_invalidation_hours=0.0,
    )
    shr = SignalHitRate(
        signal_type="volume", window=TrailingWindow.FIVE_DAYS, cited_count=0, validated_count=0
    )
    stc = SignalToThesisConversion(
        signal_type="volume",
        window=TrailingWindow.FIVE_DAYS,
        signal_observed_count=0,
        pm_approved_count=0,
    )
    cc = ConvictionCalibrationEntry(
        conviction_level=1,
        window=TrailingWindow.FIVE_DAYS,
        count=0,
        validation_rate=0.0,
        mean_realized_pnl_pct=0.0,
    )
    csd = ConvictionSizingDeviation(
        window=TrailingWindow.FIVE_DAYS,
        total_proposals=0,
        pm_sized_above_advisory_count=0,
        pm_sized_below_advisory_count=0,
        pm_sized_within_advisory_count=0,
        outcome_correlation_above=None,
        outcome_correlation_below=None,
    )
    pa = PerformanceAttributionEntry(
        dimension=AttributionDimension.SECTOR,
        key="Technology",
        window=TrailingWindow.FIVE_DAYS,
        cumulative_realized_pnl_usd=0.0,
        realized_pnl_pct_of_window_capital=0.0,
        count=0,
    )
    ab = AlphaBetaDecomposition(
        window=TrailingWindow.FIVE_DAYS,
        total_realized_pnl_usd=0.0,
        market_component_usd=0.0,
        sector_component_usd=0.0,
        alpha_component_usd=0.0,
    )
    return ThesisQualityAggregate(
        as_of_timestamp=_T0,
        resolution_counts_by_window=(rwc,),
        duration_stats_by_window=(dur,),
        invalidation_timing_stats_by_window=(inv_timing,),
        signal_hit_rates=(shr,),
        signal_to_thesis_conversions=(stc,),
        conviction_calibration=(cc,),
        conviction_sizing_deviation_by_window=(csd,),
        performance_attribution=(pa,),
        alpha_beta_decomposition_by_window=(ab,),
    )


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=signed_money(500.0),
        total_unrealized_pnl_pct_of_portfolio=1.5,
        daily_realized_pnl_usd=signed_money(200.0),
        daily_total_pnl_usd=signed_money(700.0),
        cumulative_realized_pnl_usd=signed_money(10000.0),
        rolling_realized_pnl={
            "1d": signed_money(200.0),
            "3d": signed_money(600.0),
            "5d": signed_money(1000.0),
            "20d": signed_money(3000.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=signed_money(0.0),
        total_short_delta_adjusted_usd=signed_money(0.0),
        net_directional_pct_of_portfolio=0.0,
        gross_pct_of_portfolio=0.0,
    )


def _make_sector_entry() -> SectorExposureEntry:
    return SectorExposureEntry(
        sector="TECHNOLOGY",
        long_delta_adjusted_usd=signed_money(10000.0),
        short_delta_adjusted_usd=signed_money(0.0),
        long_pct_of_portfolio=20.0,
        short_pct_of_portfolio=0.0,
        long_short_ratio=None,
    )


def _make_snapshot(**overrides: object) -> PortfolioStateSnapshot:
    pos1 = _make_open_position("POS-001", "AAPL")
    pos2 = _make_open_position("POS-002", "MSFT")
    brk1 = _make_bracket("BRK-001", "POS-001")
    brk2 = _make_bracket("BRK-002", "POS-002")
    order1 = _make_pending_order("ORD-001", "POS-001")
    thesis1 = _make_thesis("THESIS-001", "POS-001")
    changelog = _make_changelog_entry("ENTRY-CL-001", "POS-001")
    pm_entry = _make_pm_decision_entry("ENTRY-PM-001")
    mod_entry = _make_changelog_entry("ENTRY-MOD-001", "POS-001")
    base: dict[str, Any] = {
        "invocation_id": _INV_ID,
        "fill_collection_committed_at": _T0,
        "snapshot_assembled_at": _T1,
        "pipeline_invocation_started_at": _T2,
        "open_positions": (pos1, pos2),
        "pending_positions": (),
        "sector_exposure": (_make_sector_entry(),),
        "directional_exposure": _make_directional(),
        "portfolio_pnl": _make_pnl(),
        "drawdown": _make_drawdown_state(),
        "active_theses": (thesis1,),
        "recent_thesis_resolutions": (),
        "cash_ledger": _make_cash_ledger(),
        "pending_orders": (order1,),
        "risk_budget": _make_risk_budget(),
        "active_risk_parameters": _make_active_risk_params(),
        "intra_invocation_changelog": (changelog,),
        "recent_pm_decision_log": (pm_entry,),
        "position_modification_trail": {"POS-001": (mod_entry,)},
        "thesis_quality_aggregates": _make_thesis_quality(),
        "brackets": (brk1, brk2),
    }
    base.update(overrides)
    return PortfolioStateSnapshot(**base)


def _make_empty_snapshot() -> PortfolioStateSnapshot:
    return PortfolioStateSnapshot(
        invocation_id=_INV_ID,
        fill_collection_committed_at=_T0,
        snapshot_assembled_at=_T1,
        pipeline_invocation_started_at=None,
        open_positions=(),
        pending_positions=(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown_state(),
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=_make_cash_ledger(),
        pending_orders=(),
        risk_budget=_make_risk_budget(),
        active_risk_parameters=_make_active_risk_params(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=_make_thesis_quality(),
        brackets=(),
    )


# ---------------------------------------------------------------------------
# Tests: value object models
# ---------------------------------------------------------------------------


class TestPortfolioManagerValueObjects:
    def test_view_frozen(self) -> None:
        snapshot = _make_snapshot()
        view = project_portfolio_manager_view(snapshot)
        with pytest.raises(FrozenInstanceError):
            view.positions = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: project_portfolio_manager_view — happy path
# ---------------------------------------------------------------------------


class TestProjectPortfolioManagerViewHappyPath:
    def test_includes_all_strategist_fields(self) -> None:
        """PM view must have all the same fields as StrategistView."""
        snapshot = _make_snapshot()
        view = project_portfolio_manager_view(snapshot)
        # All strategist fields present
        assert hasattr(view, "positions")
        assert hasattr(view, "portfolio_pnl")
        assert hasattr(view, "drawdown")
        assert hasattr(view, "sector_exposure")
        assert hasattr(view, "directional_exposure")
        assert hasattr(view, "risk_budget")
        assert hasattr(view, "active_risk_parameters")
        assert hasattr(view, "intra_invocation_changelog")
        assert hasattr(view, "recent_pm_decision_log")
        assert hasattr(view, "abandoned_openings")
        assert hasattr(view, "abandoned_actions")
        assert hasattr(view, "recent_thesis_resolutions")

    def test_includes_thesis_quality_aggregates(self) -> None:
        snapshot = _make_snapshot()
        view = project_portfolio_manager_view(snapshot)
        assert view.thesis_quality_aggregates is snapshot.thesis_quality_aggregates

    def test_includes_position_modification_trail_dict(self) -> None:
        """PM view has full per-position trail dict at top level."""
        snapshot = _make_snapshot()
        view = project_portfolio_manager_view(snapshot)
        assert "POS-001" in view.position_modification_trail
        assert len(view.position_modification_trail["POS-001"]) == 1

    def test_positions_from_snapshot(self) -> None:
        snapshot = _make_snapshot()
        view = project_portfolio_manager_view(snapshot)
        assert len(view.positions) == 2

    def test_thesis_quality_carried_through(self) -> None:
        snapshot = _make_snapshot()
        view = project_portfolio_manager_view(snapshot)
        assert view.thesis_quality_aggregates.as_of_timestamp == _T0


# ---------------------------------------------------------------------------
# Tests: project_portfolio_manager_view — empty portfolio
# ---------------------------------------------------------------------------


class TestProjectPortfolioManagerViewEmpty:
    def test_empty_positions(self) -> None:
        view = project_portfolio_manager_view(_make_empty_snapshot())
        assert view.positions == ()

    def test_empty_modification_trail(self) -> None:
        view = project_portfolio_manager_view(_make_empty_snapshot())
        assert view.position_modification_trail == {}

    def test_no_exception(self) -> None:
        # Should not raise
        project_portfolio_manager_view(_make_empty_snapshot())


class TestProjectPortfolioManagerViewDeterminism:
    def test_identical_calls_produce_equal_output(self) -> None:
        snapshot = _make_snapshot()
        view1 = project_portfolio_manager_view(snapshot)
        view2 = project_portfolio_manager_view(snapshot)
        assert view1 == view2


class TestSnapshotBackedThesisComponentReader:
    def test_isinstance_check_passes(self) -> None:
        snapshot = _make_snapshot()
        reader = SnapshotBackedThesisComponentReader(snapshot)
        assert isinstance(reader, PortfolioManagerThesisComponentReader)

    def test_returns_components_for_known_position(self) -> None:
        snapshot = _make_snapshot()
        reader = SnapshotBackedThesisComponentReader(snapshot)
        result = asyncio.run(reader.get_thesis_components("POS-001"))
        assert len(result) == 3  # 3 components in _make_thesis

    def test_returns_empty_tuple_for_unknown_position(self) -> None:
        snapshot = _make_snapshot()
        reader = SnapshotBackedThesisComponentReader(snapshot)
        result = asyncio.run(reader.get_thesis_components("NONEXISTENT"))
        assert result == ()

    def test_returns_empty_tuple_for_position_without_thesis(self) -> None:
        snapshot = _make_snapshot()
        reader = SnapshotBackedThesisComponentReader(snapshot)
        # POS-002 has no active thesis
        result = asyncio.run(reader.get_thesis_components("POS-002"))
        assert result == ()

    def test_never_raises_for_empty_snapshot(self) -> None:
        reader = SnapshotBackedThesisComponentReader(_make_empty_snapshot())
        result = asyncio.run(reader.get_thesis_components("any"))
        assert result == ()
