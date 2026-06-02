"""Tests for the portfolio manager guardrail state header renderer (story 04c)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from itertools import pairwise

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    DrawdownTier,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
    PositionReducedDetail,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    SectorExposureEntry,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery import (
    CorrelationState,
    CrossConstraintImpact,
    CrossConstraintImpactPerRule,
    DependencyRiskFlag,
    RegimeOverride,
    render_pm_header,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from tests.risk_guardrails.state_delivery.fixtures import (
    _DEFAULT_POSITION_ZONES,
    _make_budget_entry,
    _make_thesis_quality_aggregates,
)

# ---------------------------------------------------------------------------
# Fixture builders (shared _DEFAULT/_make_budget hoisted to fixtures/)
# ---------------------------------------------------------------------------


def _make_active_parameters(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    parameter_change_flag: bool = False,
    daily_dd_limit_pct: float = 2.5,
    per_position_max_pct: float = 5.0,
) -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=parameter_change_flag,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown limit",
                value=daily_dd_limit_pct,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=daily_dd_limit_pct,
            ),
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Per-position max size",
                value=per_position_max_pct,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=per_position_max_pct,
            ),
        ),
        active_overlays=(),
    )


def _make_state_delivery_config(
    *,
    correlation_min: int = 3,
    dependency_min: int = 3,
) -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=1,
        correlation_state_min_position_count=correlation_min,
        dependency_risk_flag_min_position_count=dependency_min,
        abandoned_window_lookback_invocations=1,
    )


def _make_drawdown_state(
    *,
    current_pct: float = 1.5,
    daily_zone: RiskZone = RiskZone.NORMAL,
    cumulative_zone: RiskZone = RiskZone.NORMAL,
    cumulative_tier: DrawdownTier | None = None,
) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=current_pct,
        equity_high_water_mark_usd=550_000.0,
        drawdown_duration_hours=12.0,
        lifetime_max_drawdown_pct=4.5,
        intraday_drawdown_pct=current_pct,
        daily_zone=daily_zone,
        cumulative_zone=cumulative_zone,
        cumulative_tier=cumulative_tier,
        drawdown_by_source_pct={},
    )


def _make_pnl(*, daily_total_pnl_usd: float = 2_500.0) -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=money(10_000.0),
        total_unrealized_pnl_pct_of_portfolio=2.0,
        daily_realized_pnl_usd=money(500.0),
        daily_total_pnl_usd=signed_money(daily_total_pnl_usd),
        cumulative_realized_pnl_usd=money(20_000.0),
        rolling_realized_pnl={
            "1d": money(500.0),
            "3d": money(1_500.0),
            "5d": money(2_000.0),
            "20d": money(8_000.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_directional_exposure() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=money(300_000.0),
        total_short_delta_adjusted_usd=money(80_000.0),
        net_directional_pct_of_portfolio=42.0,
        gross_pct_of_portfolio=78.0,
    )


def _make_position(
    *,
    position_id: str,
    ticker: str,
    direction: Direction = Direction.LONG,
    sector: str = "tech",
    weight_pct: float = 4.2,
    unrealized_pnl_pct: float = 5.0,
) -> PositionView:
    is_short = direction == Direction.SHORT
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=100.0,
        average_cost_basis_per_share=150.0,
        borrow_rate_pct=0.5 if is_short else None,
        accrued_borrow_cost_usd=0.0 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=2_000.0 if is_short else None,
    )
    fill = PositionFill(
        fill_timestamp=datetime(2026, 4, 1, 14, 30, 0, tzinfo=UTC),
        fill_price=price(150.0),
        fill_quantity=100.0,
        slippage=signed_money(0.05),
        fees=money(1.0),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=datetime(2026, 4, 1, 14, 30, 0, tzinfo=UTC),
        details=equity,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(weight_pct * 5_000.0),
        unrealized_pnl_usd=signed_money(unrealized_pnl_pct * 100.0),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(weight_pct * 5_000.0),
        delta_adjusted_exposure_usd=signed_money(weight_pct * 5_000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _wrap_position(view: PositionView) -> StrategistPositionView:
    # Use a relaxed-validation StrategistPositionView via construct to avoid pulling
    # in thesis/bracket fixtures we do not need at the renderer layer.
    return StrategistPositionView(
        position=view,
        thesis=None,
        bracket=None,
        pending_orders=(),
        modification_trail=(),
    )


def _make_pm_view(
    *,
    positions: tuple[PositionView, ...] = (),
    risk_budget: RiskBudgetConsumption | None = None,
    active: ActiveRiskParameterSet | None = None,
    drawdown: DrawdownState | None = None,
    pnl: PortfolioPnL | None = None,
    sector_exposure: tuple[SectorExposureEntry, ...] = (),
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (),
) -> PortfolioManagerView:
    return PortfolioManagerView(
        positions=tuple(_wrap_position(p) for p in positions),
        recent_thesis_resolutions=(),
        portfolio_pnl=pnl or _make_pnl(),
        drawdown=drawdown or _make_drawdown_state(),
        sector_exposure=sector_exposure,
        directional_exposure=_make_directional_exposure(),
        risk_budget=risk_budget or _micro_risk_budget(),
        active_risk_parameters=active or _make_active_parameters(),
        intra_invocation_changelog=intra_invocation_changelog,
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=_make_thesis_quality_aggregates(),
        position_modification_trail={},
    )


def _micro_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current_value=18.3,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=12.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current_value=42.0,
                limit_value=60.0,
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                current_value=78.0,
                limit_value=120.0,
            ),
            _make_budget_entry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown",
                current_value=0.5,
                limit_value=2.5,
                unit="% of portfolio",
            ),
        )
    )


_MICRO_SECTOR_LABELS = {"tech": "Tech", "semis": "Semis"}


def _sector_resolver(position: PositionRecord) -> str | None:
    """Test-only sector resolver: maps known tickers to sectors."""
    ticker_to_sector: dict[str, str] = {
        "NVDA": "tech",
        "AAPL": "tech",
        "AMD": "tech",
        "MU": "semis",
        "AVGO": "semis",
    }
    if not isinstance(position.details, EquityPositionDetails):
        return None
    return ticker_to_sector.get(position.details.ticker)


# ---------------------------------------------------------------------------
# Tracer bullet
# ---------------------------------------------------------------------------


def test_render_pm_header_returns_string_with_envelope_open_and_close() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert isinstance(rendered, str)
    assert rendered.startswith("=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===")
    assert rendered.endswith("===")


def test_render_pm_header_renders_regime_line_after_envelope() -> None:
    view = _make_pm_view(active=_make_active_parameters(regime_label=RegimeLabel.NORMAL))
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    assert lines[1] == "Regime: normal [unchanged]"


def test_render_pm_header_renders_capital_block_after_blank() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    assert lines[2] == ""
    assert lines[3] == "Capital:"
    assert lines[4] == "  Available for new positions: $300,000 (60.0% of portfolio)"
    assert lines[5] == "  Per-position max size: $25,000 (5.0% of portfolio, normal regime)"


def test_render_pm_header_renders_sector_and_directional_headroom_blocks() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Sector headroom (delta-adjusted):" in rendered
    assert "  Tech:  18.3% / 25.0% — room: 6.7% [NORMAL]" in rendered
    assert "  Semis: 12.0% / 25.0% — room: 13.0% [NORMAL]" in rendered
    assert "Directional headroom:" in rendered
    assert "  Net long:  42.0% / 60.0% — room: 18.0%" in rendered
    assert "  Gross:     78.0% / 120.0% — room: 42.0%" in rendered


def test_render_pm_header_renders_position_level_constraint_proximity_block() -> None:
    positions = (
        _make_position(
            position_id=PositionId("POS-NVDA-001"),
            ticker=Symbol("NVDA"),
            sector="tech",
            weight_pct=4.2,
            unrealized_pnl_pct=-18.0,
        ),
        _make_position(
            position_id=PositionId("POS-AMD-002"),
            ticker=Symbol("AMD"),
            sector="tech",
            weight_pct=2.1,
            unrealized_pnl_pct=5.0,
        ),
    )
    view = _make_pm_view(positions=positions)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Position-level constraint proximity:" in rendered
    assert (
        "  POS-NVDA-001:  4.2% of portfolio (max 5.0%) — P/L: -18.0% of cost [⚠ WARNING: size]"
        in rendered
    )
    assert "  POS-AMD-002:   2.1% of portfolio (max 5.0%) — P/L: +5.0% of cost" in rendered


def test_render_pm_header_critical_from_size_not_positive_pnl() -> None:
    """ALP-580 regression: debug-pos-07 (weight 4.5%, P/L +19900%) in the PM header.

    The CRITICAL tag must attribute to size proximity (4.5% / 5.0% = 90%), not
    to the +19900% P/L — confirms the PM header shares the strategist's
    signed-comparison loss zone via render_position_proximity_block.
    """
    positions = (
        _make_position(
            position_id=PositionId("POS-NVDA-001"),
            ticker=Symbol("NVDA"),
            sector="tech",
            weight_pct=4.5,
            unrealized_pnl_pct=19_900.0,
        ),
    )
    view = _make_pm_view(positions=positions)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    proximity_line = next(
        line for line in rendered.splitlines() if line.lstrip().startswith("POS-NVDA-001:")
    )
    # An exact `: size]` tag rules out the `: loss]` and `: size+loss]` variants.
    assert proximity_line.endswith("[\U0001f534 CRITICAL: size]")


def test_render_pm_header_renders_sector_exposure_breakdown_per_position() -> None:
    positions = (
        _make_position(
            position_id=PositionId("POS-NVDA-001"),
            ticker=Symbol("NVDA"),
            sector="tech",
            weight_pct=4.2,
            unrealized_pnl_pct=-1.0,
        ),
        _make_position(
            position_id=PositionId("POS-AAPL-002"),
            ticker=Symbol("AAPL"),
            sector="tech",
            weight_pct=3.1,
            unrealized_pnl_pct=2.0,
        ),
        _make_position(
            position_id=PositionId("POS-MU-003"),
            ticker=Symbol("MU"),
            sector="semis",
            weight_pct=2.5,
            unrealized_pnl_pct=4.0,
        ),
    )
    view = _make_pm_view(positions=positions)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Sector exposure breakdown (per position):" in rendered
    assert "  Tech (18.3% / 25.0%):" in rendered
    assert "    POS-NVDA-001: 4.2% (delta-adj)" in rendered
    assert "    POS-AAPL-002: 3.1% (delta-adj)" in rendered
    assert "  Semis (12.0% / 25.0%):" in rendered
    assert "    POS-MU-003: 2.5% (delta-adj)" in rendered


def test_render_pm_header_renders_cross_constraint_impact_empty_per_rule() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Cross-constraint impact summary:" in rendered
    assert "  No pending proposals; no projected impact." in rendered


def test_render_pm_header_renders_cross_constraint_impact_with_per_rule_lines() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="sector_concentration_tech",
                rule_label="Sector tech",
                current=18.3,
                projected_after=22.1,
                limit=25.0,
                unit="% of portfolio (delta-adjusted)",
                status="PASS",
                headroom_remaining=2.9,
            ),
            CrossConstraintImpactPerRule(
                rule_id="net_long_pct",
                rule_label="Net long",
                current=42.0,
                projected_after=48.5,
                limit=60.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=11.5,
            ),
            CrossConstraintImpactPerRule(
                rule_id="gross_exposure_pct",
                rule_label="Gross",
                current=78.0,
                projected_after=84.5,
                limit=120.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=35.5,
            ),
        ),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(240_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Cross-constraint impact summary:" in rendered
    assert "  If all pending proposals are approved as-sized:" in rendered
    assert "    Sector tech: 18.3% → 22.1% (within limit, 2.9% headroom)" in rendered
    assert "    Net long:    42.0% → 48.5% (within limit, 11.5% headroom)" in rendered
    assert "    Gross:       78.0% → 84.5% (within limit, 35.5% headroom)" in rendered
    assert "    Capital:     $300,000 → $240,000" in rendered
    # No flagged-suffix line.
    assert "Flagged" not in rendered


def test_render_pm_header_cross_constraint_impact_breach_status() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="sector_concentration_tech",
                rule_label="Sector tech",
                current=18.3,
                projected_after=27.5,
                limit=25.0,
                unit="% of portfolio (delta-adjusted)",
                status="FAIL",
                headroom_remaining=-2.5,
            ),
        ),
        flagged_rule_ids=("sector_concentration_tech",),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(240_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "    Sector tech: 18.3% → 27.5% [BREACH] would breach by 2.5%" in rendered
    assert "    Flagged: Sector tech" in rendered


def _render_with_single_rule(rule: CrossConstraintImpactPerRule) -> str:
    """Render the PM header with one cross-constraint rule fixture."""
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(rule,),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(240_000.0),
    )
    return render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )


def test_render_pm_header_inverse_rule_pass_reads_status_not_arithmetic() -> None:
    # ALP-622 regression: inverse-rule (floor) with projected ABOVE the floor
    # is PASS — the legacy "projected_after <= limit" arithmetic inverted it.
    # min_cash_reserve_pct at 27.1% vs 10.0% floor renders as compliant with
    # 17.1% headroom, not as a 17.1% breach.
    rule = CrossConstraintImpactPerRule(
        rule_id="min_cash_reserve_pct",
        rule_label="Min cash reserve",
        current=27.1,
        projected_after=27.1,
        limit=10.0,
        unit="% of portfolio",
        status="PASS",
        headroom_remaining=17.1,
    )
    rendered = _render_with_single_rule(rule)
    assert "    Min cash reserve: 27.1% → 27.1% (within limit, 17.1% headroom)" in rendered
    assert "would breach" not in rendered


def test_render_pm_header_escalation_zone_fail_renders_breach_marker() -> None:
    # ALP-622 regression: a cap-style rule numerically under its limit but
    # inside the hard-block escalation zone must render a breach marker —
    # net_long_pct at 57.2% / 60.0% with pre-processor status=FAIL.
    rule = CrossConstraintImpactPerRule(
        rule_id="net_long_pct",
        rule_label="Net long",
        current=57.2,
        projected_after=57.2,
        limit=60.0,
        unit="% of portfolio",
        status="FAIL",
        headroom_remaining=2.8,
    )
    rendered = _render_with_single_rule(rule)
    assert "[BREACH]" in rendered
    assert "(within limit," not in rendered
    assert "    Net long: 57.2% → 57.2% [BREACH] in hard-block zone, 2.8% headroom" in rendered


def test_render_pm_header_at_limit_fail_renders_breach_marker() -> None:
    # ALP-622 regression: position_max_size_pct at 5.0% vs 5.0% with status=FAIL
    # must render a breach marker (legacy arithmetic rendered it as
    # `(within limit)` because projected_after == limit).
    rule = CrossConstraintImpactPerRule(
        rule_id="position_max_size_pct",
        rule_label="Position max size",
        current=5.0,
        projected_after=5.0,
        limit=5.0,
        unit="% of portfolio",
        status="FAIL",
        headroom_remaining=0.0,
    )
    rendered = _render_with_single_rule(rule)
    assert "[BREACH]" in rendered
    assert "(within limit," not in rendered


def test_render_pm_header_warning_status_renders_distinct_marker() -> None:
    rule = CrossConstraintImpactPerRule(
        rule_id="gross_exposure_pct",
        rule_label="Gross",
        current=100.0,
        projected_after=105.0,
        limit=120.0,
        unit="% of portfolio",
        status="WARNING",
        headroom_remaining=15.0,
    )
    rendered = _render_with_single_rule(rule)
    assert "⚠ WARNING" in rendered
    assert "[BREACH]" not in rendered
    assert "(within limit," not in rendered
    assert "    Gross:   100.0% → 105.0% [⚠ WARNING] within limit, 15.0% headroom" in rendered


def test_render_pm_header_inverse_rule_fail_renders_shortfall() -> None:
    # Inverse rule below its floor — projected_after < limit, headroom < 0.
    rule = CrossConstraintImpactPerRule(
        rule_id="min_cash_reserve_pct",
        rule_label="Min cash reserve",
        current=12.0,
        projected_after=7.5,
        limit=10.0,
        unit="% of portfolio",
        status="FAIL",
        headroom_remaining=-2.5,
    )
    rendered = _render_with_single_rule(rule)
    assert "[BREACH] would breach by 2.5%" in rendered


def test_render_pm_header_renders_validation_tool_reminder_block_verbatim() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Guardrail validation tool available:" in rendered
    assert (
        "  Call validate_guardrail(instrument, direction, size) to check any proposed modification."
    ) in rendered
    assert (
        "  Tool tracks cumulative impact across multiple checks within this invocation."
    ) in rendered


def test_render_pm_header_renders_drawdown_context_with_signed_daily_pnl_positive() -> None:
    view = _make_pm_view(pnl=_make_pnl(daily_total_pnl_usd=2_500.0))  # +0.5%
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Drawdown context:" in rendered
    assert "  Daily P/L:     +0.5% (NORMAL)" in rendered
    assert "  Daily limit:   2.5% — headroom: 1.0%" in rendered
    assert "  Cumulative:    1.5% from HWM (NORMAL)" in rendered


def test_render_pm_header_drawdown_context_negative_daily_pnl() -> None:
    view = _make_pm_view(pnl=_make_pnl(daily_total_pnl_usd=-6_000.0))  # -1.2%
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "  Daily P/L:     -1.2% (NORMAL)" in rendered


def test_render_pm_header_drawdown_context_zero_daily_pnl_signed_positive() -> None:
    view = _make_pm_view(pnl=_make_pnl(daily_total_pnl_usd=0.0))
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "  Daily P/L:     +0.0% (NORMAL)" in rendered


def test_render_pm_header_drawdown_context_with_cumulative_tier() -> None:
    view = _make_pm_view(
        drawdown=_make_drawdown_state(
            current_pct=8.5,
            cumulative_zone=RiskZone.WARNING,
            cumulative_tier=DrawdownTier.CONSTRAINED,
        ),
    )
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "  Cumulative:    8.5% from HWM (⚠ WARNING)" in rendered
    assert (
        "  Cumulative tier: constrained — max position size 3%, "
        "max gross 80%, positions w/ unrealized loss > 10% flagged" in rendered
    )


def test_render_pm_header_regime_transition_breaches_block_present_when_breaches_exist() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    breach = RegimeTransitionBreach(
        position_id=PositionId("POS-NVDA-001"),
        rule_id="position_max_size_pct",
        rule_label="Per-position max size",
        current_value=4.2,
        new_limit_value=3.5,
        overage=0.7,
        unit="% of portfolio",
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        regime_transition_breaches=(breach,),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Regime-transition breaches" in rendered
    assert "POS-NVDA-001" in rendered
    assert "4.2" in rendered
    assert "3.5" in rendered


def test_render_pm_header_regime_transition_breaches_block_omitted_when_empty() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Regime-transition breaches" not in rendered


def _engine_close_entry(
    *,
    entry_id: str = "ALE-001",
    position_id: str = "POS-XYZ-001",
    timestamp: datetime | None = None,
    realized_pnl_usd: float = -3_100.0,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id="inv-001",
        timestamp=timestamp or datetime(2026, 4, 28, 14, 0, 0, tzinfo=UTC),
        event_type=EventType.POSITION_CLOSED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER,
        detail=PositionClosedDetail(
            exit_method=PositionExitMethod.STOP_TRIGGERED,
            exit_price=money("120.0"),
            realized_pnl_usd=signed_money(realized_pnl_usd),
            thesis_resolution_category="position_level_max_loss",
        ),
    )


def _engine_reduce_entry(
    *,
    entry_id: str = "ALE-002",
    position_id: str = "POS-ABC-002",
    timestamp: datetime | None = None,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id="inv-001",
        timestamp=timestamp or datetime(2026, 4, 28, 14, 5, 0, tzinfo=UTC),
        event_type=EventType.POSITION_REDUCED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER,
        detail=PositionReducedDetail(
            reduced_quantity=20.0,
            partial_realized_pnl_usd=signed_money("-100.0"),
            close_rationale_classification="single_short_size_limit",
        ),
    )


def test_render_pm_header_recent_engine_actions_block_none_when_changelog_empty() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Recent engine-originated actions (since last invocation):" in rendered
    lines = rendered.splitlines()
    header_idx = lines.index("Recent engine-originated actions (since last invocation):")
    assert lines[header_idx + 1] == "  None"


def test_render_pm_header_recent_engine_actions_block_renders_close_and_trim_in_order() -> None:
    close_entry = _engine_close_entry(
        timestamp=datetime(2026, 4, 28, 14, 0, 0, tzinfo=UTC),
    )
    trim_entry = _engine_reduce_entry(
        timestamp=datetime(2026, 4, 28, 14, 5, 0, tzinfo=UTC),
    )
    view = _make_pm_view(intra_invocation_changelog=(close_entry, trim_entry))
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    header_idx = lines.index("Recent engine-originated actions (since last invocation):")
    assert lines[header_idx + 1].startswith("  2026-04-28T14:00:00Z: Engine closed POS-XYZ-001 — ")
    assert "position_level_max_loss" in lines[header_idx + 1]
    assert lines[header_idx + 2].startswith("  2026-04-28T14:05:00Z: Engine trimmed POS-ABC-002 — ")
    assert "single_short_size_limit" in lines[header_idx + 2]


def test_render_pm_header_active_regime_overrides_block_none_when_empty() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Active regime overrides:" in rendered
    lines = rendered.splitlines()
    header_idx = lines.index("Active regime overrides:")
    assert lines[header_idx + 1] == "  None"


def test_render_pm_header_active_regime_overrides_block_with_expiry_appends_suffix() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    expiry = datetime(2026, 4, 29, 21, 0, 0, tzinfo=UTC)
    overlay = RegimeOverride(
        overlay_name="pre_event_tightening",
        description=(
            "FOMC tightening — max position size -20%, no new positions in final invocation"
        ),
        expires_at=expiry,
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        active_regime_overrides=(overlay,),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Active regime overrides:" in rendered
    assert (
        "  FOMC tightening — max position size -20%, no new positions in final invocation "
        "(expires 2026-04-29T21:00:00Z)"
    ) in rendered


def test_render_pm_header_active_regime_overrides_block_without_expiry_no_suffix() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    overlay = RegimeOverride(
        overlay_name="stress_overlay",
        description="VIX spike — gross exposure -25%",
        expires_at=None,
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        active_regime_overrides=(overlay,),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "  VIX spike — gross exposure -25%" in rendered
    assert "(expires" not in rendered


def _three_position_view() -> PortfolioManagerView:
    positions = (
        _make_position(
            position_id=PositionId("POS-NVDA-001"), ticker=Symbol("NVDA"), weight_pct=4.2
        ),
        _make_position(
            position_id=PositionId("POS-AAPL-002"), ticker=Symbol("AAPL"), weight_pct=3.1
        ),
        _make_position(
            position_id=PositionId("POS-MU-003"),
            ticker=Symbol("MU"),
            sector="semis",
            weight_pct=2.5,
        ),
    )
    return _make_pm_view(positions=positions)


def test_render_pm_header_correlation_state_block_omitted_when_state_is_none() -> None:
    view = _three_position_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        correlation_state=None,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Correlation state:" not in rendered


def test_render_pm_header_correlation_state_block_omitted_when_below_position_threshold() -> None:
    view = _make_pm_view(
        positions=(
            _make_position(
                position_id=PositionId("POS-NVDA-001"), ticker=Symbol("NVDA"), weight_pct=4.2
            ),
            _make_position(
                position_id=PositionId("POS-AAPL-002"), ticker=Symbol("AAPL"), weight_pct=3.1
            ),
        ),
    )
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    correlation = CorrelationState(
        weighted_avg_correlation=0.62,
        correlation_limit=0.70,
        zone=RiskZone.WARNING,
        highest_pairwise_position_a="POS-NVDA-001",
        highest_pairwise_position_b="POS-AAPL-002",
        highest_pairwise_value=0.85,
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(correlation_min=3),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        correlation_state=correlation,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Correlation state:" not in rendered


def test_render_pm_header_correlation_state_block_renders_when_threshold_met() -> None:
    view = _three_position_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    correlation = CorrelationState(
        weighted_avg_correlation=0.62,
        correlation_limit=0.70,
        zone=RiskZone.WARNING,
        highest_pairwise_position_a="POS-NVDA-001",
        highest_pairwise_position_b="POS-AAPL-002",
        highest_pairwise_value=0.85,
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(correlation_min=3),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        correlation_state=correlation,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Correlation state:" in rendered
    assert "  Portfolio weighted avg correlation: 0.62 / 0.70 [⚠ WARNING]" in rendered
    assert "  Highest pairwise: POS-NVDA-001 ↔ POS-AAPL-002 = 0.85" in rendered


def test_render_pm_header_dependency_risk_flag_block_omitted_when_none_or_below_threshold() -> None:
    view = _three_position_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered_none = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(dependency_min=3),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        dependency_risk_flag=None,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Dependency risk flag:" not in rendered_none

    flag = DependencyRiskFlag(
        max_catalyst_failure_exposure_pct=18.0,
        catalyst_failure_limit_pct=25.0,
        zone=RiskZone.NORMAL,
        effective_independent_thesis_count=4,
        worst_shared_catalyst_label="FOMC June rate cut",
        worst_shared_catalyst_position_ids=("POS-NVDA-001", "POS-AAPL-002"),
    )
    two_position_view = _make_pm_view(
        positions=(
            _make_position(
                position_id=PositionId("POS-NVDA-001"), ticker=Symbol("NVDA"), weight_pct=4.2
            ),
            _make_position(
                position_id=PositionId("POS-AAPL-002"), ticker=Symbol("AAPL"), weight_pct=3.1
            ),
        ),
    )
    rendered_below = render_pm_header(
        pm_view=two_position_view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(dependency_min=3),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        dependency_risk_flag=flag,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Dependency risk flag:" not in rendered_below


def test_render_pm_header_dependency_risk_flag_block_renders_when_threshold_met() -> None:
    view = _three_position_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    flag = DependencyRiskFlag(
        max_catalyst_failure_exposure_pct=18.0,
        catalyst_failure_limit_pct=25.0,
        zone=RiskZone.NORMAL,
        effective_independent_thesis_count=4,
        worst_shared_catalyst_label="FOMC June rate cut",
        worst_shared_catalyst_position_ids=("POS-NVDA-001", "POS-AAPL-002"),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(dependency_min=3),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        dependency_risk_flag=flag,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Dependency risk flag:" in rendered
    assert "  Max catalyst-failure exposure: 18.0% / 25.0% [NORMAL]" in rendered
    assert "  Effective independent thesis count: 4" in rendered
    assert (
        '  Worst shared catalyst: "FOMC June rate cut" — positions: {POS-NVDA-001, POS-AAPL-002}'
    ) in rendered


def test_render_pm_header_hard_blocks_uses_pm_specific_header_label() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=(
            *_micro_risk_budget().entries[:1],  # tech only, in CRITICAL
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=24.2,
                limit_value=25.0,
                zone=RiskZone.CRITICAL,
            ),
            *_micro_risk_budget().entries[2:],
        )
    )
    view = _make_pm_view(risk_budget=risk_budget)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Hard blocks (do NOT issue commands violating):" in rendered
    assert "Hard blocks (do NOT recommend):" not in rendered
    assert "  Semis sector concentration at 24.2% / 25.0% limit" in rendered
    # Disabled features still listed
    assert "  Options: DISABLED for this portfolio" in rendered
    assert "  Short selling: DISABLED for this portfolio" in rendered


def test_render_pm_header_omits_hard_blocks_when_no_breaches_and_features_enabled() -> None:
    # Add a position so dependency-risk-flag has data; risk-budget has no breaches.
    full_risk_budget = RiskBudgetConsumption(
        entries=(
            *_micro_risk_budget().entries,
            _make_budget_entry(
                rule_id="net_short_pct",
                rule_label="Net short exposure",
                current_value=10.0,
                limit_value=30.0,
            ),
            _make_budget_entry(
                rule_id="options_delta_pct",
                rule_label="Options delta exposure",
                current_value=22.0,
                limit_value=40.0,
            ),
            _make_budget_entry(
                rule_id="portfolio_theta_pct_per_day",
                rule_label="Portfolio theta",
                current_value=0.10,
                limit_value=0.18,
                unit="% of portfolio per day",
            ),
            _make_budget_entry(
                rule_id="portfolio_vega_pct_per_iv_point",
                rule_label="Portfolio vega",
                current_value=0.6,
                limit_value=1.0,
                unit="% of portfolio per vol point",
            ),
        )
    )
    view = _make_pm_view(risk_budget=full_risk_budget)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Hard blocks" not in rendered
    lines = rendered.splitlines()
    assert lines[-1] == "==="
    assert lines[-2] != ""


def test_render_pm_header_raises_when_options_disabled_but_options_rule_present() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=(
            *_micro_risk_budget().entries,
            _make_budget_entry(
                rule_id="options_delta_pct",
                rule_label="Options delta exposure",
                current_value=22.0,
                limit_value=40.0,
            ),
        )
    )
    view = _make_pm_view(risk_budget=risk_budget)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    with pytest.raises(ValueError, match="options_delta_pct"):
        render_pm_header(
            pm_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_sector_resolver,
            total_portfolio_value_usd=money(500_000.0),
            available_for_new_positions_usd=money(300_000.0),
            cross_constraint_impact=impact,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_pm_header_raises_when_short_selling_disabled_but_net_short_present() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=(
            *_micro_risk_budget().entries,
            _make_budget_entry(
                rule_id="net_short_pct",
                rule_label="Net short exposure",
                current_value=10.0,
                limit_value=30.0,
            ),
        )
    )
    view = _make_pm_view(risk_budget=risk_budget)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    with pytest.raises(ValueError, match="net_short_pct"):
        render_pm_header(
            pm_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_sector_resolver,
            total_portfolio_value_usd=money(500_000.0),
            available_for_new_positions_usd=money(300_000.0),
            cross_constraint_impact=impact,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_pm_header_raises_when_position_max_size_pct_missing() -> None:
    active = ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown limit",
                value=2.5,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=2.5,
            ),
        ),
        active_overlays=(),
    )
    view = _make_pm_view(active=active)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    with pytest.raises(ValueError, match="position_max_size_pct"):
        render_pm_header(
            pm_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_sector_resolver,
            total_portfolio_value_usd=money(500_000.0),
            available_for_new_positions_usd=money(300_000.0),
            cross_constraint_impact=impact,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_pm_header_raises_when_net_long_pct_missing_from_risk_budget() -> None:
    risk_budget = RiskBudgetConsumption(
        entries=tuple(
            entry for entry in _micro_risk_budget().entries if entry.rule_id != "net_long_pct"
        )
    )
    view = _make_pm_view(risk_budget=risk_budget)
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    with pytest.raises(ValueError, match="net_long_pct"):
        render_pm_header(
            pm_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_sector_resolver,
            total_portfolio_value_usd=money(500_000.0),
            available_for_new_positions_usd=money(300_000.0),
            cross_constraint_impact=impact,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_pm_header_raises_when_active_sector_missing_from_risk_budget() -> None:
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    with pytest.raises(ValueError, match="sector_concentration_financials"):
        render_pm_header(
            pm_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis", "financials"),
            config=_make_state_delivery_config(),
            sector_label_display={"tech": "Tech", "semis": "Semis", "financials": "Financials"},
            sector_resolver=_sector_resolver,
            total_portfolio_value_usd=money(500_000.0),
            available_for_new_positions_usd=money(300_000.0),
            cross_constraint_impact=impact,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_pm_header_is_deterministic() -> None:
    view = _three_position_view()
    impact = CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="sector_concentration_tech",
                rule_label="Sector tech",
                current=18.3,
                projected_after=22.1,
                limit=25.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=2.9,
            ),
        ),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(240_000.0),
    )
    timestamp = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC)
    config = _make_state_delivery_config()

    def _invoke() -> str:
        return render_pm_header(
            pm_view=view,
            invocation_id="inv-002",
            timestamp=timestamp,
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=config,
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_sector_resolver,
            total_portfolio_value_usd=money(500_000.0),
            available_for_new_positions_usd=money(300_000.0),
            cross_constraint_impact=impact,
            position_zones=_DEFAULT_POSITION_ZONES,
        )

    assert _invoke() == _invoke()


def test_render_pm_header_micro_fixture_full_render() -> None:
    """Micro: 3 positions, no overlays, correlation+dependency-risk omitted, empty impact."""
    view = _three_position_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        # correlation_state_min_position_count=4 forces correlation block omission
        # dependency_risk_flag_min_position_count=4 forces dependency block omission
        config=_make_state_delivery_config(correlation_min=4, dependency_min=4),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    expected = "\n".join(
        [
            "=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===",
            "Regime: normal [unchanged]",
            "",
            "Capital:",
            "  Available for new positions: $300,000 (60.0% of portfolio)",
            "  Per-position max size: $25,000 (5.0% of portfolio, normal regime)",
            "",
            "Sector headroom (delta-adjusted):",
            "  Tech:  18.3% / 25.0% — room: 6.7% [NORMAL]",
            "  Semis: 12.0% / 25.0% — room: 13.0% [NORMAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Position-level constraint proximity:",
            "  POS-NVDA-001:  4.2% of portfolio (max 5.0%) — P/L: +5.0% of cost [⚠ WARNING: size]",
            "  POS-AAPL-002:  3.1% of portfolio (max 5.0%) — P/L: +5.0% of cost",
            "  POS-MU-003:    2.5% of portfolio (max 5.0%) — P/L: +5.0% of cost",
            "",
            "Sector exposure breakdown (per position):",
            "  Tech (18.3% / 25.0%):",
            "    POS-NVDA-001: 4.2% (delta-adj)",
            "    POS-AAPL-002: 3.1% (delta-adj)",
            "  Semis (12.0% / 25.0%):",
            "    POS-MU-003: 2.5% (delta-adj)",
            "",
            "Cross-constraint impact summary:",
            "  No pending proposals; no projected impact.",
            "",
            "Guardrail validation tool available:",
            "  Call validate_guardrail(instrument, direction, size) "
            "to check any proposed modification.",
            "  Tool tracks cumulative impact across multiple checks within this invocation.",
            "",
            "Drawdown context:",
            "  Daily P/L:     +0.5% (NORMAL)",
            "  Daily limit:   2.5% — headroom: 1.0%",
            "  Cumulative:    1.5% from HWM (NORMAL)",
            "",
            "Recent engine-originated actions (since last invocation):",
            "  None",
            "",
            "Active regime overrides:",
            "  None",
            "",
            "Hard blocks (do NOT issue commands violating):",
            "  Options: DISABLED for this portfolio",
            "  Short selling: DISABLED for this portfolio",
            "===",
        ]
    )
    assert rendered == expected


def test_render_pm_header_full_system_fixture_full_render() -> None:
    """Full-system: 12 positions, 3 pending proposals (impact non-empty), 1 overlay,
    correlation in WARNING, dependency-risk in NORMAL.
    """
    positions = (
        _make_position(
            position_id=PositionId("POS-NVDA-001"), ticker=Symbol("NVDA"), weight_pct=4.2
        ),
        _make_position(
            position_id=PositionId("POS-AAPL-002"), ticker=Symbol("AAPL"), weight_pct=3.5
        ),
        _make_position(position_id=PositionId("POS-AMD-003"), ticker=Symbol("AMD"), weight_pct=2.1),
        _make_position(
            position_id=PositionId("POS-AVGO-004"),
            ticker=Symbol("AVGO"),
            sector="semis",
            weight_pct=3.0,
        ),
        _make_position(
            position_id=PositionId("POS-MU-005"),
            ticker=Symbol("MU"),
            sector="semis",
            weight_pct=2.5,
        ),
    )
    full_risk_budget = RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current_value=18.3,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=12.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current_value=42.0,
                limit_value=60.0,
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                current_value=78.0,
                limit_value=120.0,
            ),
            _make_budget_entry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown",
                current_value=0.5,
                limit_value=2.5,
                unit="% of portfolio",
            ),
        )
    )
    view = _make_pm_view(positions=positions, risk_budget=full_risk_budget)
    impact = CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="sector_concentration_tech",
                rule_label="Sector tech",
                current=18.3,
                projected_after=22.1,
                limit=25.0,
                unit="% of portfolio (delta-adjusted)",
                status="PASS",
                headroom_remaining=2.9,
            ),
            CrossConstraintImpactPerRule(
                rule_id="net_long_pct",
                rule_label="Net long",
                current=42.0,
                projected_after=48.5,
                limit=60.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=11.5,
            ),
            CrossConstraintImpactPerRule(
                rule_id="gross_exposure_pct",
                rule_label="Gross",
                current=78.0,
                projected_after=84.5,
                limit=120.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=35.5,
            ),
        ),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(240_000.0),
    )
    overlay = RegimeOverride(
        overlay_name="pre_event_tightening",
        description=(
            "FOMC tightening — max position size -20%, no new positions in final invocation"
        ),
        expires_at=datetime(2026, 4, 29, 21, 0, 0, tzinfo=UTC),
    )
    correlation = CorrelationState(
        weighted_avg_correlation=0.62,
        correlation_limit=0.70,
        zone=RiskZone.WARNING,
        highest_pairwise_position_a="POS-NVDA-001",
        highest_pairwise_position_b="POS-AAPL-002",
        highest_pairwise_value=0.85,
    )
    dep = DependencyRiskFlag(
        max_catalyst_failure_exposure_pct=18.0,
        catalyst_failure_limit_pct=25.0,
        zone=RiskZone.NORMAL,
        effective_independent_thesis_count=4,
        worst_shared_catalyst_label="FOMC June rate cut",
        worst_shared_catalyst_position_ids=("POS-NVDA-001", "POS-AAPL-002"),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-002",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(correlation_min=3, dependency_min=3),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        active_regime_overrides=(overlay,),
        correlation_state=correlation,
        dependency_risk_flag=dep,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    expected = "\n".join(
        [
            "=== GUARDRAIL STATE (invocation inv-002, 2026-04-28T14:32:05Z) ===",
            "Regime: normal [unchanged]",
            "",
            "Capital:",
            "  Available for new positions: $300,000 (60.0% of portfolio)",
            "  Per-position max size: $25,000 (5.0% of portfolio, normal regime)",
            "",
            "Sector headroom (delta-adjusted):",
            "  Tech:  18.3% / 25.0% — room: 6.7% [NORMAL]",
            "  Semis: 12.0% / 25.0% — room: 13.0% [NORMAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Position-level constraint proximity:",
            "  POS-NVDA-001:  4.2% of portfolio (max 5.0%) — P/L: +5.0% of cost [⚠ WARNING: size]",
            "  POS-AAPL-002:  3.5% of portfolio (max 5.0%) — P/L: +5.0% of cost [⚠ WARNING: size]",
            "  POS-AMD-003:   2.1% of portfolio (max 5.0%) — P/L: +5.0% of cost",
            "  POS-AVGO-004:  3.0% of portfolio (max 5.0%) — P/L: +5.0% of cost",
            "  POS-MU-005:    2.5% of portfolio (max 5.0%) — P/L: +5.0% of cost",
            "",
            "Sector exposure breakdown (per position):",
            "  Tech (18.3% / 25.0%):",
            "    POS-NVDA-001: 4.2% (delta-adj)",
            "    POS-AAPL-002: 3.5% (delta-adj)",
            "    POS-AMD-003: 2.1% (delta-adj)",
            "  Semis (12.0% / 25.0%):",
            "    POS-AVGO-004: 3.0% (delta-adj)",
            "    POS-MU-005: 2.5% (delta-adj)",
            "",
            "Cross-constraint impact summary:",
            "  If all pending proposals are approved as-sized:",
            "    Sector tech: 18.3% → 22.1% (within limit, 2.9% headroom)",
            "    Net long:    42.0% → 48.5% (within limit, 11.5% headroom)",
            "    Gross:       78.0% → 84.5% (within limit, 35.5% headroom)",
            "    Capital:     $300,000 → $240,000",
            "",
            "Guardrail validation tool available:",
            "  Call validate_guardrail(instrument, direction, size) "
            "to check any proposed modification.",
            "  Tool tracks cumulative impact across multiple checks within this invocation.",
            "",
            "Drawdown context:",
            "  Daily P/L:     +0.5% (NORMAL)",
            "  Daily limit:   2.5% — headroom: 1.0%",
            "  Cumulative:    1.5% from HWM (NORMAL)",
            "",
            "Recent engine-originated actions (since last invocation):",
            "  None",
            "",
            "Active regime overrides:",
            "  FOMC tightening — max position size -20%, no new positions in final "
            "invocation (expires 2026-04-29T21:00:00Z)",
            "",
            "Correlation state:",
            "  Portfolio weighted avg correlation: 0.62 / 0.70 [⚠ WARNING]",
            "  Highest pairwise: POS-NVDA-001 ↔ POS-AAPL-002 = 0.85",
            "",
            "Dependency risk flag:",
            "  Max catalyst-failure exposure: 18.0% / 25.0% [NORMAL]",
            "  Effective independent thesis count: 4",
            '  Worst shared catalyst: "FOMC June rate cut" — '
            "positions: {POS-NVDA-001, POS-AAPL-002}",
            "",
            "Hard blocks (do NOT issue commands violating):",
            "  Options: DISABLED for this portfolio",
            "  Short selling: DISABLED for this portfolio",
            "===",
        ]
    )
    assert rendered == expected


# ---------------------------------------------------------------------------
# Frozen-dataclass shape (story 10e — ALP-478)
#
# The 5 PM-header parameter-bag types are pure internal containers — they
# never cross HTTP/MCP/file/DB boundaries — so they are stdlib
# ``@dataclass(frozen=True, slots=True)`` rather than Pydantic ``BaseModel``.
# Each test below pins that shape: dataclass marker present, slots present,
# mutation raises ``FrozenInstanceError``. Render-output equivalence is
# already exercised by the suite above.
# ---------------------------------------------------------------------------


def test_cross_constraint_impact_per_rule_is_frozen_slotted_dataclass() -> None:
    rule = CrossConstraintImpactPerRule(
        rule_id="net_long_pct",
        rule_label="Net long",
        current=42.0,
        projected_after=45.0,
        limit=60.0,
        unit="% of portfolio",
        status="PASS",
        headroom_remaining=15.0,
    )
    assert dataclasses.is_dataclass(rule)
    assert hasattr(CrossConstraintImpactPerRule, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.rule_id = "other"  # type: ignore[misc]


def test_cross_constraint_impact_is_frozen_slotted_dataclass() -> None:
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(5_000.0),
        available_capital_after_usd=money(3_500.0),
    )
    assert dataclasses.is_dataclass(impact)
    assert hasattr(CrossConstraintImpact, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        impact.flagged_rule_ids = ("x",)  # type: ignore[misc]


def test_regime_override_is_frozen_slotted_dataclass() -> None:
    override = RegimeOverride(
        overlay_name="pre_event",
        description="FOMC tightening",
        expires_at=None,
    )
    assert dataclasses.is_dataclass(override)
    assert hasattr(RegimeOverride, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        override.overlay_name = "other"  # type: ignore[misc]


def test_correlation_state_is_frozen_slotted_dataclass() -> None:
    state = CorrelationState(
        weighted_avg_correlation=0.45,
        correlation_limit=0.60,
        zone=RiskZone.NORMAL,
        highest_pairwise_position_a="POS-NVDA-001",
        highest_pairwise_position_b="POS-AAPL-002",
        highest_pairwise_value=0.78,
    )
    assert dataclasses.is_dataclass(state)
    assert hasattr(CorrelationState, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.weighted_avg_correlation = 0.99  # type: ignore[misc]


def test_dependency_risk_flag_is_frozen_slotted_dataclass() -> None:
    flag = DependencyRiskFlag(
        max_catalyst_failure_exposure_pct=12.0,
        catalyst_failure_limit_pct=20.0,
        zone=RiskZone.NORMAL,
        effective_independent_thesis_count=8,
        worst_shared_catalyst_label="Q2 earnings",
        worst_shared_catalyst_position_ids=("POS-NVDA-001", "POS-AAPL-002"),
    )
    assert dataclasses.is_dataclass(flag)
    assert hasattr(DependencyRiskFlag, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        flag.effective_independent_thesis_count = 99  # type: ignore[misc]


def test_render_pm_header_has_no_double_blank_lines_and_no_trailing_blank() -> None:
    view = _three_position_view()
    impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=money(300_000.0),
        available_capital_after_usd=money(300_000.0),
    )
    rendered = render_pm_header(
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_sector_resolver,
        total_portfolio_value_usd=money(500_000.0),
        available_for_new_positions_usd=money(300_000.0),
        cross_constraint_impact=impact,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    for prev_line, next_line in pairwise(lines):
        assert not (prev_line == "" and next_line == ""), "double blank line found"
    assert not rendered.endswith("\n")
    assert lines[-1] == "==="
    assert lines[-2] != ""
