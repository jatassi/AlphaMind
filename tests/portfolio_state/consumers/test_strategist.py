"""Tests for consumers/strategist.py — story 07."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

from alphamind._kernel.ids import (
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
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
from alphamind.portfolio_state.consumers.analyst import AnalystAbandonedOpening
from alphamind.portfolio_state.consumers.strategist import (
    BetweenInvocationClosure,
    StrategistAbandonedAction,
    StrategistPositionView,
    project_strategist_view,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    CommandAbandonedDetail,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
    PositionClosedDetail,
    PositionExitMethod,
    PositionOpenedDetail,
    PositionOpenMechanism,
)
from alphamind.portfolio_state.records.positions import (
    InstrumentType,
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
_INV_ID = "inv-strat-001"


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


def _make_abandoned_entry(
    entry_id: str = "ENTRY-ABN-001",
    originating_agent: str = "analyst",
    command_id: str = "cmd-abn-001",
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"] = "ADD",
) -> ActivityLogEntry:
    detail = CommandAbandonedDetail(
        envelope_id="env-abn-001",
        command_id=command_id,
        originating_agent=originating_agent,
        command_type=command_type,
        failure_reason="Guardrail blocked",
        retry_attempt_count=0,
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INV_ID,
        timestamp=_T0,
        event_type=EventType.COMMAND_ABANDONED,
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
        total_long_delta_adjusted_usd=signed_money(50000.0),
        total_short_delta_adjusted_usd=signed_money(0.0),
        net_directional_pct_of_portfolio=50.0,
        gross_pct_of_portfolio=50.0,
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


class TestStrategistValueObjects:
    def test_position_view_frozen(self) -> None:
        pos = _make_open_position()
        view = StrategistPositionView(
            position=pos,
            thesis=None,
            bracket=None,
            pending_orders=(),
            modification_trail=(),
        )
        with pytest.raises(FrozenInstanceError):
            view.thesis = None  # type: ignore[misc]

    def test_abandoned_action_frozen(self) -> None:
        action = StrategistAbandonedAction(
            envelope_id="env-001",
            command_type="ADD",
            position_id=PositionId("POS-001"),
            order_id=None,
            abandoned_at=_T0,
            failure_reason="blocked",
        )
        with pytest.raises(FrozenInstanceError):
            action.envelope_id = "other"  # type: ignore[misc]

    def test_strategist_view_frozen(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        with pytest.raises(FrozenInstanceError):
            view.positions = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: project_strategist_view — happy path
# ---------------------------------------------------------------------------


class TestProjectStrategistViewHappyPath:
    def test_positions_length(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert len(view.positions) == 2

    def test_position_view_bundles_thesis(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        pos1_view = next(pv for pv in view.positions if pv.position.position_id == "POS-001")
        assert pos1_view.thesis is not None
        assert pos1_view.thesis.thesis_id == "THESIS-001"

    def test_position_view_thesis_none_for_orphan(self) -> None:
        """Position with no active thesis gets thesis=None (orphan/spin-off case)."""
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        pos2_view = next(pv for pv in view.positions if pv.position.position_id == "POS-002")
        assert pos2_view.thesis is None

    def test_position_view_bundles_bracket(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        pos1_view = next(pv for pv in view.positions if pv.position.position_id == "POS-001")
        assert pos1_view.bracket is not None
        assert pos1_view.bracket.bracket_id == "BRK-001"

    def test_position_view_bracket_none_without_bracket(self) -> None:
        """Position with no bracket gets bracket=None."""
        pos = _make_open_position("POS-NOBRK", "TSLA")
        snapshot = _make_snapshot(
            open_positions=(_make_open_position("POS-001"), pos),
            brackets=(_make_bracket("BRK-001", "POS-001"),),
        )
        view = project_strategist_view(snapshot)
        nobrk_view = next(pv for pv in view.positions if pv.position.position_id == "POS-NOBRK")
        assert nobrk_view.bracket is None

    def test_position_view_bundles_pending_orders(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        pos1_view = next(pv for pv in view.positions if pv.position.position_id == "POS-001")
        assert len(pos1_view.pending_orders) == 1
        assert pos1_view.pending_orders[0].order_id == "ORD-001"

    def test_position_view_modification_trail(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        pos1_view = next(pv for pv in view.positions if pv.position.position_id == "POS-001")
        assert len(pos1_view.modification_trail) == 1
        assert pos1_view.modification_trail[0].entry_id == "ENTRY-MOD-001"

    def test_position_view_empty_trail_for_untrailed_position(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        pos2_view = next(pv for pv in view.positions if pv.position.position_id == "POS-002")
        assert pos2_view.modification_trail == ()

    def test_view_has_portfolio_pnl(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert view.portfolio_pnl is snapshot.portfolio_pnl

    def test_view_has_drawdown(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert view.drawdown is snapshot.drawdown

    def test_view_has_sector_exposure(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert view.sector_exposure == snapshot.sector_exposure

    def test_view_has_risk_budget(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert view.risk_budget is snapshot.risk_budget

    def test_view_has_active_risk_parameters(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert view.active_risk_parameters is snapshot.active_risk_parameters

    def test_view_has_changelog(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert view.intra_invocation_changelog == snapshot.intra_invocation_changelog

    def test_view_has_pm_decision_log(self) -> None:
        snapshot = _make_snapshot()
        view = project_strategist_view(snapshot)
        assert view.recent_pm_decision_log == snapshot.recent_pm_decision_log

    def test_abandoned_openings_from_analyst(self) -> None:
        """Strategist sees analyst abandoned openings (for portfolio awareness)."""
        abn = _make_abandoned_entry("ENTRY-ABN-001", "analyst")
        changelog = _make_changelog_entry("ENTRY-CL-001", "POS-001")
        snapshot = _make_snapshot(intra_invocation_changelog=(changelog, abn))
        view = project_strategist_view(snapshot)
        assert len(view.abandoned_openings) == 1
        assert isinstance(view.abandoned_openings[0], AnalystAbandonedOpening)

    def test_abandoned_actions_from_strategist(self) -> None:
        """StrategistAbandonedAction entries come from strategist-originated abandons."""
        abn = _make_abandoned_entry("ENTRY-ABN-STR", "strategist")
        changelog = _make_changelog_entry("ENTRY-CL-001", "POS-001")
        snapshot = _make_snapshot(intra_invocation_changelog=(changelog, abn))
        view = project_strategist_view(snapshot)
        assert len(view.abandoned_actions) == 1
        action = view.abandoned_actions[0]
        assert isinstance(action, StrategistAbandonedAction)
        assert action.failure_reason == "Guardrail blocked"

    @pytest.mark.parametrize("command_type", ["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"])
    def test_abandoned_action_command_type_threaded_from_detail(
        self,
        command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"],
    ) -> None:
        """command_type must come from the source CommandAbandonedDetail, not be hard-coded."""
        abn = _make_abandoned_entry("ENTRY-ABN-STR", "strategist", command_type=command_type)
        changelog = _make_changelog_entry("ENTRY-CL-001", "POS-001")
        snapshot = _make_snapshot(intra_invocation_changelog=(changelog, abn))
        view = project_strategist_view(snapshot)
        assert len(view.abandoned_actions) == 1
        assert view.abandoned_actions[0].command_type == command_type


# ---------------------------------------------------------------------------
# Tests: project_strategist_view — empty portfolio
# ---------------------------------------------------------------------------


class TestProjectStrategistViewEmpty:
    def test_empty_positions(self) -> None:
        view = project_strategist_view(_make_empty_snapshot())
        assert view.positions == ()

    def test_empty_sector_exposure(self) -> None:
        view = project_strategist_view(_make_empty_snapshot())
        assert view.sector_exposure == ()

    def test_empty_activity_log(self) -> None:
        view = project_strategist_view(_make_empty_snapshot())
        assert view.intra_invocation_changelog == ()
        assert view.recent_pm_decision_log == ()

    def test_empty_abandoned_fields(self) -> None:
        view = project_strategist_view(_make_empty_snapshot())
        assert view.abandoned_openings == ()
        assert view.abandoned_actions == ()


class TestProjectStrategistViewDeterminism:
    def test_identical_calls_produce_equal_output(self) -> None:
        snapshot = _make_snapshot()
        view1 = project_strategist_view(snapshot)
        view2 = project_strategist_view(snapshot)
        assert view1 == view2


# ---------------------------------------------------------------------------
# Tests: between_invocation_closures projection (story 04c / ALP-440)
# ---------------------------------------------------------------------------


def _make_position_closed_entry(
    *,
    entry_id: str,
    position_id: str,
    timestamp: datetime,
    source: EventSource,
    exit_method: PositionExitMethod,
    order_id: str | None = None,
    exit_price: float = 150.0,
    realized_pnl_usd: float = -100.0,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INV_ID,
        timestamp=timestamp,
        event_type=EventType.POSITION_CLOSED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=order_id,
        thesis_id=None,
        source=source,
        detail=PositionClosedDetail(
            exit_method=exit_method,
            exit_price=money(exit_price),
            realized_pnl_usd=signed_money(realized_pnl_usd),
            thesis_resolution_category="invalidated",
        ),
    )


class TestBetweenInvocationClosuresProjection:
    def test_bracket_manager_closure_projected(self) -> None:
        """BRACKET_MANAGER-sourced POSITION_CLOSED projects with origin='bracket_manager'."""
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-BRK",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.BRACKET_MANAGER,
            exit_method=PositionExitMethod.STOP_TRIGGERED,
            order_id=OrderId("ord-close-1"),
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        assert len(view.between_invocation_closures) == 1
        closure = view.between_invocation_closures[0]
        assert isinstance(closure, BetweenInvocationClosure)
        assert closure.position_id == "POS-001"
        assert closure.exit_method is PositionExitMethod.STOP_TRIGGERED
        assert closure.origin == "bracket_manager"
        assert closure.closing_order_id == "ord-close-1"

    def test_target_reached_closure_projected(self) -> None:
        """BRACKET_MANAGER + TARGET_REACHED projects with the matching exit_method."""
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-TGT",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.BRACKET_MANAGER,
            exit_method=PositionExitMethod.TARGET_REACHED,
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        assert len(view.between_invocation_closures) == 1
        assert view.between_invocation_closures[0].exit_method is PositionExitMethod.TARGET_REACHED

    def test_margin_monitor_closure_projected(self) -> None:
        """MARGIN_MONITOR POSITION_CLOSED projects with origin='margin_monitor'."""
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-MGN",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.MARGIN_MONITOR,
            exit_method=PositionExitMethod.MARGIN_LIQUIDATION,
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        assert len(view.between_invocation_closures) == 1
        closure = view.between_invocation_closures[0]
        assert closure.origin == "margin_monitor"
        assert closure.exit_method is PositionExitMethod.MARGIN_LIQUIDATION

    def test_guardrail_layer_closure_projected(self) -> None:
        """GUARDRAIL_LAYER-sourced POSITION_CLOSED projects with origin='guardrail_layer'."""
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-GR",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.GUARDRAIL_LAYER,
            exit_method=PositionExitMethod.PM_DECISION,
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        assert len(view.between_invocation_closures) == 1
        assert view.between_invocation_closures[0].origin == "guardrail_layer"

    def test_engine_guardrail_provenance_closure_projected(self) -> None:
        """PM_DECISION with source_provenance='engine_guardrail' surfaces resulting POSITION_CLOSED.

        Cascade closures from 04a flow through the engine-envelope submission path and
        emit POSITION_CLOSED with COMMAND_EXECUTOR as the row source, BUT carry an
        engine_guardrail-provenance PM_DECISION envelope referencing them. The projection
        joins on resulting_command_ids so the closure appears with origin='engine_guardrail'.
        """
        pm_entry = ActivityLogEntry(
            entry_id="ENTRY-PM-EG",
            invocation_id=_INV_ID,
            timestamp=_T0,
            event_type=EventType.PM_DECISION,
            event_group=EventGroup.PM_DECISION,
            position_id=PositionId("POS-001"),
            order_id=None,
            thesis_id=None,
            source=EventSource.COMMAND_EXECUTOR,
            detail=PMDecisionDetail(
                envelope_id="env-eg-001",
                source_provenance_json={
                    "source_provenance": "engine_guardrail",
                    "source_recommendation_id": None,
                    "recommendation_type": None,
                    "position_id": "POS-001",
                },
                evaluation_json={},
                modifications_json=[],
                resulting_command_ids=("MON.mon-001.1.0",),
                verdict=PMVerdict.APPROVE,
                originating_proposal_json={},
            ),
        )
        close_entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-EG",
            position_id=PositionId("POS-001"),
            timestamp=_T1,
            source=EventSource.COMMAND_EXECUTOR,
            exit_method=PositionExitMethod.PM_DECISION,
            order_id=OrderId("MON.mon-001.1.0"),
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(pm_entry, close_entry))
        view = project_strategist_view(snapshot)
        assert len(view.between_invocation_closures) == 1
        closure = view.between_invocation_closures[0]
        assert closure.origin == "engine_guardrail"
        assert closure.position_id == "POS-001"

    def test_mixed_sources_chronological_order(self) -> None:
        """Mix of bracket_manager and engine_guardrail closures returns in chronological order."""
        bracket_entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-1",
            position_id=PositionId("POS-001"),
            timestamp=_T2,  # later
            source=EventSource.BRACKET_MANAGER,
            exit_method=PositionExitMethod.STOP_TRIGGERED,
        )
        pm_entry = ActivityLogEntry(
            entry_id="ENTRY-PM-EG-2",
            invocation_id=_INV_ID,
            timestamp=_T0,
            event_type=EventType.PM_DECISION,
            event_group=EventGroup.PM_DECISION,
            position_id=PositionId("POS-002"),
            order_id=None,
            thesis_id=None,
            source=EventSource.COMMAND_EXECUTOR,
            detail=PMDecisionDetail(
                envelope_id="env-eg-002",
                source_provenance_json={
                    "source_provenance": "engine_guardrail",
                    "source_recommendation_id": None,
                    "recommendation_type": None,
                    "position_id": "POS-002",
                },
                evaluation_json={},
                modifications_json=[],
                resulting_command_ids=("MON.mon-001.2.0",),
                verdict=PMVerdict.APPROVE,
                originating_proposal_json={},
            ),
        )
        eg_close = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-EG-2",
            position_id=PositionId("POS-002"),
            timestamp=_T1,  # earlier than bracket_entry
            source=EventSource.COMMAND_EXECUTOR,
            exit_method=PositionExitMethod.PM_DECISION,
            order_id=OrderId("MON.mon-001.2.0"),
        )
        snapshot = _make_snapshot(
            intra_invocation_changelog=(bracket_entry, pm_entry, eg_close),
        )
        view = project_strategist_view(snapshot)
        assert len(view.between_invocation_closures) == 2
        # Chronological order: _T1 (eg_close) before _T2 (bracket_entry).
        assert view.between_invocation_closures[0].position_id == "POS-002"
        assert view.between_invocation_closures[0].origin == "engine_guardrail"
        assert view.between_invocation_closures[1].position_id == "POS-001"
        assert view.between_invocation_closures[1].origin == "bracket_manager"

    def test_empty_changelog_empty_tuple(self) -> None:
        view = project_strategist_view(_make_empty_snapshot())
        assert view.between_invocation_closures == ()

    def test_position_closed_with_unrelated_source_not_projected(self) -> None:
        """FILL_PROCESSOR POSITION_CLOSED (normal PM-driven close) is NOT projected."""
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-FP",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.FILL_PROCESSOR,
            exit_method=PositionExitMethod.PM_DECISION,
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        assert view.between_invocation_closures == ()

    def test_closure_includes_ticker_for_equity(self) -> None:
        """BetweenInvocationClosure.ticker is resolved from the open-positions tuple."""
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-T",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.BRACKET_MANAGER,
            exit_method=PositionExitMethod.STOP_TRIGGERED,
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        assert view.between_invocation_closures[0].ticker == "AAPL"
        assert view.between_invocation_closures[0].instrument_type is InstrumentType.EQUITY

    def test_between_invocation_closure_frozen(self) -> None:
        closure = BetweenInvocationClosure(
            position_id=PositionId("POS-001"),
            ticker=Symbol("AAPL"),
            instrument_type=InstrumentType.EQUITY,
            closed_at=_T0,
            closing_order_id="ord-1",
            exit_method=PositionExitMethod.STOP_TRIGGERED,
            origin="bracket_manager",
            rationale="price-based stop fired",
        )
        with pytest.raises(FrozenInstanceError):
            closure.position_id = "POS-002"  # type: ignore[misc]

    def test_rationale_renders_em_dash_for_zero_exit_price(self) -> None:
        """Strategy-position closures emit ``exit_price=0.0`` (Phase 1
        reconciliation overwrites the persisted P/L with the actual fill once
        the order lands). Rendering as ``$0.00`` reads as "filled at zero"
        and is operator-confusing; the rationale uses ``"—"`` instead.
        """
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-STRAT",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.BRACKET_MANAGER,
            exit_method=PositionExitMethod.STOP_TRIGGERED,
            exit_price=0.0,
            realized_pnl_usd=0.0,
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        rationale = view.between_invocation_closures[0].rationale
        assert "exit_price=—" in rationale
        assert "exit_price=$0.00" not in rationale

    def test_rationale_renders_dollar_for_positive_exit_price(self) -> None:
        """Single-leg options closures supply a real ``exit_price`` (prior
        premium per contract) and render as the dollar string unchanged.
        """
        entry = _make_position_closed_entry(
            entry_id="ENTRY-CLOSE-OPT",
            position_id=PositionId("POS-001"),
            timestamp=_T0,
            source=EventSource.BRACKET_MANAGER,
            exit_method=PositionExitMethod.STOP_TRIGGERED,
            exit_price=12.75,
            realized_pnl_usd=275.0,
        )
        snapshot = _make_snapshot(intra_invocation_changelog=(entry,))
        view = project_strategist_view(snapshot)
        rationale = view.between_invocation_closures[0].rationale
        assert "exit_price=$12.75" in rationale
