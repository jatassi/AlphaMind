"""Tests for the halt-mode guardrail state header wrappers (story 05)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import pytest

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
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
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    AnalystAvailableCapital,
    AnalystHeldPosition,
    AnalystView,
)
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.consumers.strategist import (
    StrategistAbandonedAction,
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.orders import (
    EquityInstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
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
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.state_delivery import (
    CrossConstraintImpact,
    CrossConstraintImpactPerRule,
    render_analyst_header,
    render_analyst_header_halt_mode,
    render_pm_header_halt_mode,
    render_strategist_header,
    render_strategist_header_halt_mode,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------


def _make_state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
    )


def _make_budget_entry(
    *,
    rule_id: str,
    rule_label: str,
    current_value: float,
    limit_value: float,
    zone: RiskZone = RiskZone.NORMAL,
    unit: str = "% of portfolio",
) -> RiskBudgetEntry:
    headroom = limit_value - current_value
    headroom_pct = max(0.0, min(100.0, (headroom / limit_value) * 100.0)) if limit_value else 0.0
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=headroom_pct,
        zone=zone,
        unit=unit,
        cumulative_invocation_impact_value=0.0,
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
                current_value=12.1,
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
                current_value=2.5,
                limit_value=2.5,
                zone=RiskZone.BLOCKED,
                unit="% of portfolio",
            ),
        )
    )


def _make_active_parameters(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    parameter_change_flag: bool = False,
    daily_dd_limit_pct: float = 2.5,
    cumulative_dd_limit_pct: float = 8.0,
    per_position_max_pct: float = 5.0,
    include_max_loss_equity: bool = True,
    include_max_loss_options: bool = False,
) -> ActiveRiskParameterSet:
    entries: list[ActiveRiskParameterEntry] = [
        ActiveRiskParameterEntry(
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            value=per_position_max_pct,
            unit="% of portfolio",
            regime_multiplier_applied=1.0,
            base_value=per_position_max_pct,
        ),
        ActiveRiskParameterEntry(
            rule_id="daily_drawdown_pct",
            rule_label="Daily drawdown",
            value=daily_dd_limit_pct,
            unit="% of portfolio",
            regime_multiplier_applied=1.0,
            base_value=daily_dd_limit_pct,
        ),
        ActiveRiskParameterEntry(
            rule_id="cumulative_drawdown_pct",
            rule_label="Cumulative drawdown",
            value=cumulative_dd_limit_pct,
            unit="% of portfolio",
            regime_multiplier_applied=1.0,
            base_value=cumulative_dd_limit_pct,
        ),
    ]
    if include_max_loss_equity:
        entries.append(
            ActiveRiskParameterEntry(
                rule_id="position_max_loss_equity_pct",
                rule_label="Equity max loss",
                value=30.0,
                unit="% of cost",
                regime_multiplier_applied=1.0,
                base_value=30.0,
            )
        )
    if include_max_loss_options:
        entries.append(
            ActiveRiskParameterEntry(
                rule_id="position_max_loss_options_pct",
                rule_label="Options max loss",
                value=80.0,
                unit="% of cost",
                regime_multiplier_applied=1.0,
                base_value=80.0,
            )
        )
    return ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=parameter_change_flag,
        entries=tuple(entries),
        active_overlays=(),
    )


def _make_drawdown(
    *,
    intraday: float = 2.5,
    cumulative: float = 4.0,
    daily_zone: RiskZone = RiskZone.BLOCKED,
    cumulative_zone: RiskZone = RiskZone.NORMAL,
    cumulative_tier: DrawdownTier | None = None,
) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=cumulative,
        equity_high_water_mark_usd=500_000.0,
        drawdown_duration_hours=4.0,
        lifetime_max_drawdown_pct=10.0,
        intraday_drawdown_pct=intraday,
        daily_zone=daily_zone,
        cumulative_zone=cumulative_zone,
        cumulative_tier=cumulative_tier,
        drawdown_by_source_pct={},
    )


def _make_pnl(*, daily_total_pnl_usd: float = -12_500.0) -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=signed_money(-10_000.0),
        total_unrealized_pnl_pct_of_portfolio=-2.0,
        daily_realized_pnl_usd=signed_money(-2_500.0),
        daily_total_pnl_usd=signed_money(daily_total_pnl_usd),
        cumulative_realized_pnl_usd=money(20_000.0),
        rolling_realized_pnl={
            "1d": signed_money(-2_500.0),
            "3d": signed_money(-1_500.0),
            "5d": money(0.0),
            "20d": money(8_000.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=money(210_000.0),
        total_short_delta_adjusted_usd=money(50_000.0),
        net_directional_pct_of_portfolio=32.0,
        gross_pct_of_portfolio=78.0,
    )


def _make_thesis_quality_aggregates() -> ThesisQualityAggregate:
    return ThesisQualityAggregate(
        as_of_timestamp=datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC),
        resolution_counts_by_window=(),
        duration_stats_by_window=(),
        invalidation_timing_stats_by_window=(),
        signal_hit_rates=(),
        signal_to_thesis_conversions=(),
        conviction_calibration=(),
        conviction_sizing_deviation_by_window=(),
        performance_attribution=(),
        alpha_beta_decomposition_by_window=(),
    )


# ---------------------------------------------------------------------------
# Analyst-specific fixture builders
# ---------------------------------------------------------------------------


def _make_analyst_capital() -> AnalystAvailableCapital:
    return AnalystAvailableCapital(
        available_for_new_positions_usd=300_000.0,
        available_for_new_positions_pct=60.0,
        per_position_max_size_usd=25_000.0,
        per_position_max_size_pct=5.0,
    )


def _make_analyst_view(
    *,
    held_positions: tuple[AnalystHeldPosition, ...] = (),
    abandoned_openings: tuple[AnalystAbandonedOpening, ...] = (),
) -> AnalystView:
    return AnalystView(
        held_positions=held_positions,
        active_thesis_summaries=(),
        available_capital=_make_analyst_capital(),
        pending_orders=(),
        abandoned_openings=abandoned_openings,
    )


_MICRO_SECTOR_LABELS = {"tech": "Tech", "semis": "Semis"}


# ---------------------------------------------------------------------------
# Strategist-specific fixture builders
# ---------------------------------------------------------------------------


def _make_equity_position(
    *,
    position_id: str,
    direction: Direction = Direction.LONG,
    position_weight_pct: float = 3.0,
    unrealized_pnl_pct: float = -2.0,
) -> PositionView:
    is_short = direction == Direction.SHORT
    equity_details = EquityPositionDetails(
        ticker=Symbol(position_id.split("-")[1]),
        share_count=100.0,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=1.0 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=5_000.0 if is_short else None,
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=datetime(2026, 4, 27, 14, 0, 0, tzinfo=UTC),
        details=equity_details,
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 4, 27, 14, 0, 0, tzinfo=UTC),
                fill_price=price(100.0),
                fill_quantity=100.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(position_weight_pct * 5_000.0),
        unrealized_pnl_usd=signed_money(-1_800.0),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=position_weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(position_weight_pct * 5_000.0),
        delta_adjusted_exposure_usd=signed_money(position_weight_pct * 5_000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _wrap_position(view: PositionView) -> StrategistPositionView:
    return StrategistPositionView(
        position=view,
        thesis=None,
        bracket=None,
        pending_orders=(),
        modification_trail=(),
    )


def _make_strategist_view(
    *,
    positions: tuple[StrategistPositionView, ...] = (),
    drawdown: DrawdownState | None = None,
    risk_budget: RiskBudgetConsumption | None = None,
    active: ActiveRiskParameterSet | None = None,
    abandoned_openings: tuple[AnalystAbandonedOpening, ...] = (),
    abandoned_actions: tuple[StrategistAbandonedAction, ...] = (),
    sector_exposure: tuple[SectorExposureEntry, ...] = (),
) -> StrategistView:
    return StrategistView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=drawdown or _make_drawdown(),
        sector_exposure=sector_exposure,
        directional_exposure=_make_directional(),
        risk_budget=risk_budget or _micro_risk_budget(),
        active_risk_parameters=active or _make_active_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=abandoned_openings,
        abandoned_actions=abandoned_actions,
    )


def _make_strategist_sector_resolver() -> SectorResolver:
    sector_map = {
        "POS-NVDA-001": "tech",
        "POS-AAPL-002": "tech",
        "POS-MU-003": "semis",
    }

    def _resolver(position: PositionRecord) -> str | None:
        return sector_map.get(position.position_id)

    return _resolver


# ---------------------------------------------------------------------------
# PM-specific fixture builders
# ---------------------------------------------------------------------------


def _make_pm_view(
    *,
    positions: tuple[StrategistPositionView, ...] = (),
    risk_budget: RiskBudgetConsumption | None = None,
    active: ActiveRiskParameterSet | None = None,
    drawdown: DrawdownState | None = None,
    pnl: PortfolioPnL | None = None,
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (),
) -> PortfolioManagerView:
    return PortfolioManagerView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=pnl or _make_pnl(),
        drawdown=drawdown or _make_drawdown(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        risk_budget=risk_budget or _micro_risk_budget(),
        active_risk_parameters=active or _make_active_parameters(),
        intra_invocation_changelog=intra_invocation_changelog,
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=_make_thesis_quality_aggregates(),
        position_modification_trail={},
    )


def _make_pm_sector_resolver() -> SectorResolver:
    sector_map = {
        "POS-NVDA-001": "tech",
        "POS-AAPL-002": "tech",
        "POS-MU-003": "semis",
    }

    def _resolver(position: PositionRecord) -> str | None:
        return sector_map.get(position.position_id)

    return _resolver


def _make_pm_pending_order(
    *,
    order_id: str,
    direction: OrderDirection = OrderDirection.BUY_TO_OPEN,
    ticker: str = "NVDA",
    limit_price: float = 100.0,
    quantity: float = 50.0,
    age_hours: float = 1.5,
) -> OrderRecord:
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=None,
        bracket_id=BracketId("BR-001"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(ticker)),
        direction=direction,
        order_type=OrderType.LIMIT,
        price_parameters=PriceParameters(limit_price=limit_price),
        quantity=quantity,
        duration=OrderDuration.GTC,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("ALPACA-001"),
        alpaca_order_id_chain=(AlpacaOrderId("ALPACA-001"),),
        submission_timestamp=datetime(2026, 4, 28, 13, 0, 0, tzinfo=UTC),
        last_update_timestamp=datetime(2026, 4, 28, 13, 0, 0, tzinfo=UTC),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=quantity,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=age_hours,
    )


def _make_halt_state(
    *,
    daily_halt_active: bool = True,
    cumulative_full_halt_active: bool = False,
    daily_drawdown_pct: float = 2.5,
    daily_drawdown_limit_pct: float = 2.5,
) -> HaltState:
    return HaltState(
        daily_halt_active=daily_halt_active,
        cumulative_full_halt_active=cumulative_full_halt_active,
        daily_drawdown_pct=daily_drawdown_pct,
        daily_drawdown_limit_pct=daily_drawdown_limit_pct,
    )


# ---------------------------------------------------------------------------
# Tracer bullet — analyst halt-mode wrapper produces a string with banner
# ---------------------------------------------------------------------------


def test_render_analyst_header_halt_mode_returns_string_with_banner() -> None:
    rendered = render_analyst_header_halt_mode(
        halt_state=_make_halt_state(),
        analyst_view=_make_analyst_view(),
        risk_budget=_micro_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    assert isinstance(rendered, str)
    assert rendered.startswith("=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===")
    assert "** HALT MODE ACTIVE — daily drawdown 2.5% / 2.5% **" in rendered
    assert rendered.endswith("===")


# ---------------------------------------------------------------------------
# Analyst halt-mode happy path — banner + replaced capital + preserved blocks
# ---------------------------------------------------------------------------


def _make_held_position(
    *,
    position_id: str,
    ticker: str,
    sector: str = "tech",
    direction: Direction = Direction.LONG,
    size_pct: float = 4.2,
) -> AnalystHeldPosition:
    return AnalystHeldPosition(
        position_id=position_id,
        ticker=ticker,
        direction=direction,
        sector=sector,
        size_pct=size_pct,
        instrument_type=InstrumentType.EQUITY,
        strategy_type_label=None,
    )


def test_halt_state_construction_requires_at_least_one_active() -> None:
    with pytest.raises(ValueError, match="at least one halt is active"):
        HaltState(
            daily_halt_active=False,
            cumulative_full_halt_active=False,
            daily_drawdown_pct=0.0,
            daily_drawdown_limit_pct=2.5,
        )


def test_render_analyst_header_halt_mode_full_fixture() -> None:
    held = (
        _make_held_position(
            position_id=PositionId("POS-NVDA-001"), ticker=Symbol("NVDA"), sector="tech"
        ),
        _make_held_position(
            position_id=PositionId("POS-MU-002"), ticker=Symbol("MU"), sector="semis", size_pct=2.5
        ),
    )
    view = _make_analyst_view(held_positions=held)
    rendered = render_analyst_header_halt_mode(
        halt_state=_make_halt_state(),
        analyst_view=view,
        risk_budget=_micro_risk_budget(),
        active_risk_parameters=_make_active_parameters(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
    )
    expected = "\n".join(
        [
            "=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===",
            "** HALT MODE ACTIVE — daily drawdown 2.5% / 2.5% **",
            "Mode: WATCHLIST ONLY — do not generate trade proposals",
            "Regime: normal [unchanged]",
            "",
            "Capital:",
            "  New positions: BLOCKED (halt active)",
            "  Per-position max size: not applicable (halt mode)",
            "",
            "Sector headroom (delta-adjusted):",
            "  Tech:  18.3% / 25.0% — room: 6.7% [NORMAL]",
            "  Semis: 12.1% / 25.0% — room: 12.9% [NORMAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Held positions (dedup — skip same underlying + direction; "
            "strategist owns hold/add/reduce):",
            "  NVDA  long    4.2%  Tech",
            "  MU    long    2.5%  Semis",
            "",
            "Abandoned openings from prior invocation "
            "(decide on current grounds whether to re-propose):",
            "  None",
            "",
            "Hard blocks (do NOT recommend):",
            "  Daily drawdown at 2.5% / 2.5% limit",
            "  Options: DISABLED for this portfolio",
            "  Short selling: DISABLED for this portfolio",
            "===",
        ]
    )
    assert rendered == expected


def test_render_analyst_header_halt_mode_preserves_other_blocks() -> None:
    """Regression: halt-mode output (banner + capital block stripped) matches normal output."""
    held = (
        _make_held_position(
            position_id=PositionId("POS-NVDA-001"), ticker=Symbol("NVDA"), sector="tech"
        ),
        _make_held_position(
            position_id=PositionId("POS-MU-002"), ticker=Symbol("MU"), sector="semis", size_pct=2.5
        ),
    )
    view = _make_analyst_view(held_positions=held)
    common_kwargs: dict[str, Any] = {
        "analyst_view": view,
        "risk_budget": _micro_risk_budget(),
        "active_risk_parameters": _make_active_parameters(),
        "invocation_id": "inv-001",
        "timestamp": datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": ("tech", "semis"),
        "config": _make_state_delivery_config(),
        "sector_label_display": _MICRO_SECTOR_LABELS,
    }
    halt_rendered = render_analyst_header_halt_mode(halt_state=_make_halt_state(), **common_kwargs)
    normal_rendered = render_analyst_header(**common_kwargs)
    halt_lines = halt_rendered.splitlines()
    # Drop the banner+mode lines (positions 1, 2 directly after the envelope-open).
    halt_after_banner = [halt_lines[0], *halt_lines[3:]]
    # Replace the halt-mode capital block with the normal capital block to compare the rest.
    normal_lines = normal_rendered.splitlines()
    halt_capital_idx = halt_after_banner.index("Capital:")
    normal_capital_idx = normal_lines.index("Capital:")
    halt_other = halt_after_banner[:halt_capital_idx] + halt_after_banner[halt_capital_idx + 3 :]
    normal_other = normal_lines[:normal_capital_idx] + normal_lines[normal_capital_idx + 3 :]
    assert halt_other == normal_other


# ---------------------------------------------------------------------------
# Strategist halt-mode happy path
# ---------------------------------------------------------------------------


def test_render_strategist_header_halt_mode_full_fixture() -> None:
    positions = tuple(
        _wrap_position(_make_equity_position(position_id=position_id, position_weight_pct=weight))
        for position_id, weight in (
            ("POS-NVDA-001", 3.0),
            ("POS-AAPL-002", 2.5),
            ("POS-MU-003", 2.0),
        )
    )
    view = _make_strategist_view(positions=positions)
    rendered = render_strategist_header_halt_mode(
        halt_state=_make_halt_state(),
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_strategist_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
    )
    expected = "\n".join(
        [
            "=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===",
            "** HALT MODE ACTIVE — daily drawdown 2.5% / 2.5% **",
            "Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions",
            "Regime: normal [unchanged]",
            "",
            "Capital:",
            "  Available for new positions: $300,000 (60.0% of portfolio)",
            "  Per-position max size: $25,000 (5.0% of portfolio, normal regime)",
            "",
            "Sector headroom (delta-adjusted):",
            "  Tech:  18.3% / 25.0% — room: 6.7% [NORMAL]",
            "  Semis: 12.1% / 25.0% — room: 12.9% [NORMAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Position-level constraint proximity:",
            "  POS-NVDA-001:  3.0% of portfolio (max 5.0%) — P/L: -2.0% of cost (max loss: -30.0%)",
            "  POS-AAPL-002:  2.5% of portfolio (max 5.0%) — P/L: -2.0% of cost (max loss: -30.0%)",
            "  POS-MU-003:    2.0% of portfolio (max 5.0%) — P/L: -2.0% of cost (max loss: -30.0%)",
            "",
            "Sector exposure breakdown (per position):",
            "  Tech (18.3% / 25.0%):",
            "    POS-NVDA-001: 3.0% (delta-adj)",
            "    POS-AAPL-002: 2.5% (delta-adj)",
            "  Semis (12.1% / 25.0%):",
            "    POS-MU-003: 2.0% (delta-adj)",
            "",
            "Drawdown state:",
            "  Daily:      2.5% / 2.5% [BLOCKED]",
            "  Cumulative: 4.0% / 8.0% [NORMAL]",
            "",
            "Abandoned openings from prior invocation "
            "(portfolio awareness; analyst owns re-evaluation):",
            "  None",
            "",
            "Abandoned position actions from prior invocation "
            "(decide on current grounds whether to re-propose):",
            "  None",
            "",
            "Hard blocks (do NOT recommend):",
            "  Daily drawdown at 2.5% / 2.5% limit",
            "  Options: DISABLED for this portfolio",
            "  Short selling: DISABLED for this portfolio",
            "===",
        ]
    )
    assert rendered == expected


def _make_default_cross_constraint_impact() -> CrossConstraintImpact:
    return CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current=42.0,
                projected_after=39.0,
                limit=60.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=21.0,
            ),
        ),
        flagged_rule_ids=(),
        available_capital_before_usd=300_000.0,
        available_capital_after_usd=315_000.0,
    )


def _make_pm_current_price_lookup(
    prices: dict[str, float] | None = None,
) -> Callable[[str], float]:
    table = prices or {"NVDA": 110.0}

    def _lookup(ticker: str) -> float:
        if ticker not in table:
            msg = f"price unavailable for {ticker!r}"
            raise KeyError(msg)
        return table[ticker]

    return _lookup


# ---------------------------------------------------------------------------
# PM halt-mode happy path
# ---------------------------------------------------------------------------


def test_render_pm_header_halt_mode_full_fixture() -> None:
    positions = tuple(
        _wrap_position(_make_equity_position(position_id=position_id, position_weight_pct=weight))
        for position_id, weight in (
            ("POS-NVDA-001", 3.0),
            ("POS-AAPL-002", 2.5),
        )
    )
    view = _make_pm_view(positions=positions)
    pending_orders = (
        _make_pm_pending_order(
            order_id=OrderId("ORD-1001"),
            direction=OrderDirection.BUY_TO_OPEN,
            ticker=Symbol("NVDA"),
            limit_price=100.0,
            quantity=50.0,
            age_hours=1.5,
        ),
    )
    rendered = render_pm_header_halt_mode(
        halt_state=_make_halt_state(),
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=_make_default_cross_constraint_impact(),
        pending_orders=pending_orders,
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    lines = rendered.splitlines()
    # Banner is three lines after envelope-open.
    assert lines[0] == "=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ==="
    assert lines[1] == "** HALT MODE ACTIVE — daily drawdown 2.5% / 2.5% **"
    assert lines[2] == "Available actions: CLOSE, ADJUST, CANCEL only"
    assert lines[3] == "Blocked actions: OPEN, ADD"
    # Pending orders review block should appear with the documented row format.
    pending_idx = lines.index("Pending orders review:")
    # +10% from $100 to $110
    assert lines[pending_idx + 1] == (
        "  ORD-1001: BUY_TO_OPEN NVDA @ $100.00 — current distance: +10.0% (placed 1.5h ago)"
    )
    # Cross-constraint impact has scoped variant.
    cci_idx = lines.index("Cross-constraint impact summary:")
    assert lines[cci_idx + 1] == "  Scoped to risk-reducing actions only (CLOSE, ADJUST, CANCEL)."
    # Hard blocks block has halt-mode action lines appended.
    hb_idx = lines.index("Hard blocks (do NOT issue commands violating):")
    hb_block = lines[hb_idx:]
    # The closing === marker comes after a blank — slice up to but not including the closing.
    closing_idx = hb_block.index("===") - 1  # skip blank
    hb_lines = hb_block[: closing_idx + 1]
    assert "  OPEN: BLOCKED (halt mode)" in hb_lines
    assert "  ADD: BLOCKED (halt mode)" in hb_lines
    # Closing envelope.
    assert lines[-1] == "==="


def test_render_pm_header_halt_mode_pending_orders_empty_renders_none() -> None:
    view = _make_pm_view()
    rendered = render_pm_header_halt_mode(
        halt_state=_make_halt_state(),
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=_make_default_cross_constraint_impact(),
        pending_orders=(),
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    lines = rendered.splitlines()
    pending_idx = lines.index("Pending orders review:")
    assert lines[pending_idx + 1] == "  None"


def test_render_pm_header_halt_mode_missing_price_raises_value_error() -> None:
    view = _make_pm_view()
    pending_orders = (
        _make_pm_pending_order(
            order_id=OrderId("ORD-1001"),
            ticker=Symbol("MISSING"),
            limit_price=100.0,
        ),
    )
    with pytest.raises(ValueError, match="MISSING"):
        render_pm_header_halt_mode(
            halt_state=_make_halt_state(),
            pm_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_pm_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            cross_constraint_impact=_make_default_cross_constraint_impact(),
            pending_orders=pending_orders,
            current_price_lookup=_make_pm_current_price_lookup(),
        )


def test_render_pm_header_halt_mode_cross_constraint_status_aware_breach_marker() -> None:
    # ALP-622 regression: halt-mode renderer must consume the pre-processor's
    # authoritative status, not recompute from `projected_after vs limit`.
    # Escalation-zone FAIL renders with a [BREACH] marker even though the
    # projection is numerically inside the limit.
    view = _make_pm_view()
    impact = CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="net_long_pct",
                rule_label="Net long",
                current=57.2,
                projected_after=57.2,
                limit=60.0,
                unit="% of portfolio",
                status="FAIL",
                headroom_remaining=2.8,
            ),
        ),
        flagged_rule_ids=("net_long_pct",),
        available_capital_before_usd=300_000.0,
        available_capital_after_usd=300_000.0,
    )
    rendered = render_pm_header_halt_mode(
        halt_state=_make_halt_state(),
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=impact,
        pending_orders=(),
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    assert "[BREACH] in hard-block zone, 2.8% headroom" in rendered
    assert "(within limit" not in rendered


def test_render_pm_header_halt_mode_cross_constraint_empty_emits_no_pending_line() -> None:
    view = _make_pm_view()
    empty_impact = CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=300_000.0,
        available_capital_after_usd=300_000.0,
    )
    rendered = render_pm_header_halt_mode(
        halt_state=_make_halt_state(),
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=empty_impact,
        pending_orders=(),
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    lines = rendered.splitlines()
    cci_idx = lines.index("Cross-constraint impact summary:")
    assert lines[cci_idx + 1] == "  Scoped to risk-reducing actions only (CLOSE, ADJUST, CANCEL)."
    assert lines[cci_idx + 2] == "  No pending risk-reducing actions; no projected impact."


def test_render_pm_header_halt_mode_hard_blocks_no_breaches_synthesizes_block() -> None:
    """Hard-blocks block synthesized with halt-mode lines when normal renderer would omit it."""
    # Build a budget with no breaches and feature flags True.
    no_breach_budget = RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current_value=10.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=5.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current_value=20.0,
                limit_value=60.0,
            ),
            _make_budget_entry(
                rule_id="net_short_pct",
                rule_label="Net short exposure",
                current_value=5.0,
                limit_value=30.0,
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                current_value=30.0,
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
    view = _make_pm_view(risk_budget=no_breach_budget)
    rendered = render_pm_header_halt_mode(
        halt_state=_make_halt_state(),
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=_make_default_cross_constraint_impact(),
        pending_orders=(),
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    lines = rendered.splitlines()
    hb_idx = lines.index("Hard blocks (do NOT issue commands violating):")
    assert lines[hb_idx + 1] == "  OPEN: BLOCKED (halt mode)"
    assert lines[hb_idx + 2] == "  ADD: BLOCKED (halt mode)"


def test_render_pm_header_halt_mode_hard_blocks_breaches_present_appends_action_lines() -> None:
    """Halt-mode action lines append after rendered breaches in the hard-blocks block."""
    view = _make_pm_view()  # uses _micro_risk_budget which has a BLOCKED daily-drawdown breach
    rendered = render_pm_header_halt_mode(
        halt_state=_make_halt_state(),
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=_make_default_cross_constraint_impact(),
        pending_orders=(),
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    lines = rendered.splitlines()
    hb_idx = lines.index("Hard blocks (do NOT issue commands violating):")
    assert lines[hb_idx + 1] == "  Daily drawdown at 2.5% / 2.5% limit"
    # Halt-mode action lines come after rendered breaches, before the closing envelope.
    open_idx = lines.index("  OPEN: BLOCKED (halt mode)", hb_idx)
    add_idx = lines.index("  ADD: BLOCKED (halt mode)", hb_idx)
    assert open_idx > hb_idx
    assert add_idx == open_idx + 1


def test_render_pm_header_halt_mode_hard_blocks_disabled_features_then_action_lines() -> None:
    """Disabled-feature lines precede halt-mode action lines when options/short are disabled."""
    no_breach_budget = RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current_value=10.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=5.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current_value=20.0,
                limit_value=60.0,
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                current_value=30.0,
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
    view = _make_pm_view(risk_budget=no_breach_budget)
    rendered = render_pm_header_halt_mode(
        halt_state=_make_halt_state(),
        pm_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=_make_default_cross_constraint_impact(),
        pending_orders=(),
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    lines = rendered.splitlines()
    hb_idx = lines.index("Hard blocks (do NOT issue commands violating):")
    assert lines[hb_idx + 1] == "  Options: DISABLED for this portfolio"
    assert lines[hb_idx + 2] == "  Short selling: DISABLED for this portfolio"
    assert lines[hb_idx + 3] == "  OPEN: BLOCKED (halt mode)"
    assert lines[hb_idx + 4] == "  ADD: BLOCKED (halt mode)"


def test_render_strategist_header_halt_mode_preserves_other_blocks() -> None:
    """Regression: halt-mode output minus banner equals normal output."""
    positions = tuple(
        _wrap_position(_make_equity_position(position_id=position_id, position_weight_pct=weight))
        for position_id, weight in (
            ("POS-NVDA-001", 3.0),
            ("POS-AAPL-002", 2.5),
        )
    )
    view = _make_strategist_view(positions=positions)
    common_kwargs: dict[str, Any] = {
        "strategist_view": view,
        "invocation_id": "inv-001",
        "timestamp": datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": ("tech", "semis"),
        "config": _make_state_delivery_config(),
        "sector_label_display": _MICRO_SECTOR_LABELS,
        "sector_resolver": _make_strategist_sector_resolver(),
        "total_portfolio_value_usd": 500_000.0,
        "available_for_new_positions_usd": 300_000.0,
    }
    halt_rendered = render_strategist_header_halt_mode(
        halt_state=_make_halt_state(), **common_kwargs
    )
    normal_rendered = render_strategist_header(**common_kwargs)
    halt_lines = halt_rendered.splitlines()
    # Drop the banner+mode lines (positions 1, 2).
    halt_after_banner = [halt_lines[0], *halt_lines[3:]]
    assert halt_after_banner == normal_rendered.splitlines()


# ---------------------------------------------------------------------------
# Determinism, blank-line discipline, banner-source decoupling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "render_call",
    [
        "analyst",
        "strategist",
        "pm",
    ],
)
def test_halt_mode_wrappers_are_deterministic(render_call: str) -> None:
    halt_state = _make_halt_state()
    timestamp = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC)
    config = _make_state_delivery_config()
    if render_call == "analyst":
        view = _make_analyst_view()
        kwargs: dict[str, Any] = {
            "halt_state": halt_state,
            "analyst_view": view,
            "risk_budget": _micro_risk_budget(),
            "active_risk_parameters": _make_active_parameters(),
            "invocation_id": "inv-001",
            "timestamp": timestamp,
            "options_enabled": False,
            "short_selling_enabled": False,
            "active_sectors": ("tech", "semis"),
            "config": config,
            "sector_label_display": _MICRO_SECTOR_LABELS,
        }
        first = render_analyst_header_halt_mode(**kwargs)
        second = render_analyst_header_halt_mode(**kwargs)
    elif render_call == "strategist":
        view_s = _make_strategist_view()
        kwargs_s: dict[str, Any] = {
            "halt_state": halt_state,
            "strategist_view": view_s,
            "invocation_id": "inv-001",
            "timestamp": timestamp,
            "options_enabled": False,
            "short_selling_enabled": False,
            "active_sectors": ("tech", "semis"),
            "config": config,
            "sector_label_display": _MICRO_SECTOR_LABELS,
            "sector_resolver": _make_strategist_sector_resolver(),
            "total_portfolio_value_usd": 500_000.0,
            "available_for_new_positions_usd": 300_000.0,
        }
        first = render_strategist_header_halt_mode(**kwargs_s)
        second = render_strategist_header_halt_mode(**kwargs_s)
    else:
        view_p = _make_pm_view()
        kwargs_p: dict[str, Any] = {
            "halt_state": halt_state,
            "pm_view": view_p,
            "invocation_id": "inv-001",
            "timestamp": timestamp,
            "options_enabled": False,
            "short_selling_enabled": False,
            "active_sectors": ("tech", "semis"),
            "config": config,
            "sector_label_display": _MICRO_SECTOR_LABELS,
            "sector_resolver": _make_pm_sector_resolver(),
            "total_portfolio_value_usd": 500_000.0,
            "available_for_new_positions_usd": 300_000.0,
            "cross_constraint_impact": _make_default_cross_constraint_impact(),
            "pending_orders": (),
            "current_price_lookup": _make_pm_current_price_lookup(),
        }
        first = render_pm_header_halt_mode(**kwargs_p)
        second = render_pm_header_halt_mode(**kwargs_p)
    assert first == second


@pytest.mark.parametrize(
    "render_call",
    [
        "analyst",
        "strategist",
        "pm",
    ],
)
def test_halt_mode_wrappers_have_no_double_blank_or_trailing_blank(render_call: str) -> None:
    halt_state = _make_halt_state()
    timestamp = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC)
    config = _make_state_delivery_config()
    if render_call == "analyst":
        rendered = render_analyst_header_halt_mode(
            halt_state=halt_state,
            analyst_view=_make_analyst_view(),
            risk_budget=_micro_risk_budget(),
            active_risk_parameters=_make_active_parameters(),
            invocation_id="inv-001",
            timestamp=timestamp,
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=config,
            sector_label_display=_MICRO_SECTOR_LABELS,
        )
    elif render_call == "strategist":
        rendered = render_strategist_header_halt_mode(
            halt_state=halt_state,
            strategist_view=_make_strategist_view(),
            invocation_id="inv-001",
            timestamp=timestamp,
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=config,
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_strategist_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
        )
    else:
        rendered = render_pm_header_halt_mode(
            halt_state=halt_state,
            pm_view=_make_pm_view(),
            invocation_id="inv-001",
            timestamp=timestamp,
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=config,
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_pm_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            cross_constraint_impact=_make_default_cross_constraint_impact(),
            pending_orders=(),
            current_price_lookup=_make_pm_current_price_lookup(),
        )
    lines = rendered.splitlines()
    for prev, nxt in pairwise(lines):
        assert not (prev == "" and nxt == ""), f"double blank line in {render_call}"
    assert not rendered.endswith("\n"), f"{render_call} output has trailing newline"
    assert lines[-1] == "===", f"{render_call} closing line is not ==="
    assert lines[-2] != "", f"{render_call} closing === is preceded by blank"


def test_halt_mode_banner_renders_halt_state_pcts_not_pm_view_drawdown() -> None:
    """Banner pulls daily drawdown from HaltState — not from pm_view.drawdown / risk parameters."""
    # HaltState says 100% / 100% (synthetic 100 to make decoupling unmissable).
    halt_state = _make_halt_state(daily_drawdown_pct=100.0, daily_drawdown_limit_pct=100.0)
    # PM view has 2.5% / 2.5% data (the underlying drawdown / parameters).
    rendered = render_pm_header_halt_mode(
        halt_state=halt_state,
        pm_view=_make_pm_view(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_pm_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        cross_constraint_impact=_make_default_cross_constraint_impact(),
        pending_orders=(),
        current_price_lookup=_make_pm_current_price_lookup(),
    )
    assert "** HALT MODE ACTIVE — daily drawdown 100.0% / 100.0% **" in rendered


def test_halt_mode_banner_renders_when_only_cumulative_active() -> None:
    """Cumulative-only halt: banner still renders with daily-pct values from HaltState."""
    halt_state = _make_halt_state(
        daily_halt_active=False,
        cumulative_full_halt_active=True,
        daily_drawdown_pct=0.0,
        daily_drawdown_limit_pct=2.5,
    )
    rendered = render_strategist_header_halt_mode(
        halt_state=halt_state,
        strategist_view=_make_strategist_view(),
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_strategist_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
    )
    assert "** HALT MODE ACTIVE — daily drawdown 0.0% / 2.5% **" in rendered
