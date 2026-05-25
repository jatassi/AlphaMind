"""Tests for the strategist guardrail state header renderer (story 04b)."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import pytest

from alphamind._kernel.ids import (
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
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import AnalystAbandonedOpening
from alphamind.portfolio_state.consumers.strategist import (
    StrategistAbandonedAction,
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
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
from alphamind.risk_guardrails.guardrail_evaluation.types import EscalationZones
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery import render_strategist_header
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_DEFAULT_POSITION_ZONES = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


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


def _make_param_entry(
    *,
    rule_id: str,
    rule_label: str,
    value: float,
    unit: str = "% of portfolio",
) -> ActiveRiskParameterEntry:
    return ActiveRiskParameterEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        value=value,
        unit=unit,
        regime_multiplier_applied=1.0,
        base_value=value,
    )


def _make_active_parameters(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    parameter_change_flag: bool = False,
    per_position_max_pct: float = 5.0,
    daily_drawdown_pct: float = 5.0,
    cumulative_drawdown_pct: float = 8.0,
    include_max_loss_equity: bool = True,
    include_max_loss_options: bool = True,
) -> ActiveRiskParameterSet:
    entries: list[ActiveRiskParameterEntry] = [
        _make_param_entry(
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            value=per_position_max_pct,
        ),
        _make_param_entry(
            rule_id="daily_drawdown_pct",
            rule_label="Daily drawdown",
            value=daily_drawdown_pct,
        ),
        _make_param_entry(
            rule_id="cumulative_drawdown_pct",
            rule_label="Cumulative drawdown",
            value=cumulative_drawdown_pct,
        ),
    ]
    if include_max_loss_equity:
        entries.append(
            _make_param_entry(
                rule_id="position_max_loss_equity_pct",
                rule_label="Equity max loss",
                value=30.0,
            )
        )
    if include_max_loss_options:
        entries.append(
            _make_param_entry(
                rule_id="position_max_loss_options_pct",
                rule_label="Options max loss",
                value=80.0,
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
    intraday_drawdown_pct: float = 1.5,
    current_drawdown_pct: float = 3.0,
    daily_zone: RiskZone = RiskZone.NORMAL,
    cumulative_zone: RiskZone = RiskZone.NORMAL,
    cumulative_tier: DrawdownTier | None = None,
) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=current_drawdown_pct,
        equity_high_water_mark_usd=500_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=10.0,
        intraday_drawdown_pct=intraday_drawdown_pct,
        daily_zone=daily_zone,
        cumulative_zone=cumulative_zone,
        cumulative_tier=cumulative_tier,
        drawdown_by_source_pct={},
    )


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=money(0.0),
        total_unrealized_pnl_pct_of_portfolio=0.0,
        daily_realized_pnl_usd=money(0.0),
        daily_total_pnl_usd=money(0.0),
        cumulative_realized_pnl_usd=money(0.0),
        rolling_realized_pnl={
            "1d": money(0.0),
            "3d": money(0.0),
            "5d": money(0.0),
            "20d": money(0.0),
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


def _make_equity_position(
    *,
    position_id: str,
    direction: Direction = Direction.LONG,
    position_weight_pct: float = 4.2,
    unrealized_pnl_pct: float = -18.0,
    delta_adjusted_exposure_usd: float = 21_000.0,
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
        current_market_value_usd=signed_money(21_000.0),
        unrealized_pnl_usd=signed_money(-1_800.0),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=position_weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(21_000.0),
        delta_adjusted_exposure_usd=signed_money(delta_adjusted_exposure_usd),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_options_position(
    *,
    position_id: str,
    underlying_ticker: str,
    position_weight_pct: float = 2.8,
    unrealized_pnl_pct: float = 5.0,
    delta: float = 0.45,
) -> PositionView:
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 4, 27, 14, 0, 0, tzinfo=UTC),
        details=OptionsPositionDetails(
            underlying_ticker=Symbol(underlying_ticker),
            strike_price=150.0,
            expiration_date=datetime(2026, 6, 19, tzinfo=UTC).date(),
            contract_type=OptionContractType.CALL,
            contract_count=2.0,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            premium_paid_per_contract=2_000.0,
            greeks=OptionGreeks(delta=delta, gamma=0.05, theta=-0.10, vega=0.20),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 4, 27, 14, 0, 0, tzinfo=UTC),
                fill_price=price(20.0),
                fill_quantity=2.0,
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
        current_market_value_usd=signed_money(14_000.0),
        unrealized_pnl_usd=signed_money(200.0),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=position_weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(30_000.0),
        delta_adjusted_exposure_usd=signed_money(delta * 30_000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_position_view(view: PositionView) -> StrategistPositionView:
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
    active_risk_parameters: ActiveRiskParameterSet | None = None,
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
        active_risk_parameters=active_risk_parameters or _make_active_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=abandoned_openings,
        abandoned_actions=abandoned_actions,
    )


def _micro_risk_budget(
    *,
    tech_zone: RiskZone = RiskZone.NORMAL,
    semis_zone: RiskZone = RiskZone.NORMAL,
    tech_current: float = 18.3,
) -> RiskBudgetConsumption:
    return RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current_value=tech_current,
                limit_value=25.0,
                zone=tech_zone,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
                current_value=12.1,
                limit_value=25.0,
                zone=semis_zone,
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
        )
    )


def _full_system_risk_budget() -> RiskBudgetConsumption:
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
                rule_id="sector_concentration_financials",
                rule_label="Financials sector concentration",
                current_value=8.5,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="sector_concentration_energy",
                rule_label="Energy sector concentration",
                current_value=4.0,
                limit_value=25.0,
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
                current_value=42.0,
                limit_value=60.0,
            ),
            _make_budget_entry(
                rule_id="net_short_pct",
                rule_label="Net short exposure",
                current_value=10.0,
                limit_value=30.0,
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                current_value=78.0,
                limit_value=120.0,
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


_MICRO_SECTOR_LABELS = {"tech": "Tech", "semis": "Semis"}
_FULL_SECTOR_LABELS = {
    "tech": "Tech",
    "semis": "Semis",
    "financials": "Financials",
    "energy": "Energy",
}


def _make_micro_sector_resolver() -> SectorResolver:
    sector_map = {
        "POS-NVDA-001": "tech",
        "POS-AMD-002": "tech",
        "POS-AAPL-003": "tech",
        "POS-MU-004": "semis",
        "POS-AVGO-005": "semis",
    }

    def _resolver(position: PositionRecord) -> str | None:
        return sector_map.get(position.position_id)

    return _resolver


# ---------------------------------------------------------------------------
# Tracer bullet
# ---------------------------------------------------------------------------


def test_render_strategist_header_returns_string_starting_with_envelope_open() -> None:
    view = _make_strategist_view()
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert isinstance(rendered, str)
    assert rendered.startswith("=== GUARDRAIL STATE (invocation inv-001, 2026-04-28T14:32:05Z) ===")
    assert rendered.endswith("===")


# ---------------------------------------------------------------------------
# Full-system happy path
# ---------------------------------------------------------------------------


def _full_system_strategist_positions() -> tuple[StrategistPositionView, ...]:
    """Eight positions across four sectors, including two options.

    Spec tuple shape: ``(position_id, direction, weight, pnl_pct, dae_usd)``.

    Weights intentionally < 70% of the per-position max (5.0%) so positions
    classify as NORMAL zone (no zone tag in the proximity row), except
    ``POS-NVDA-001`` at 4.2% (84%) which sits in the WARNING zone — this
    matches the regime-transition breach fixture below.
    """
    spec_equity = (
        ("POS-NVDA-001", Direction.LONG, 4.2, -18.0, 21_000.0),
        ("POS-AAPL-002", Direction.LONG, 3.0, 5.0, 15_000.0),
        ("POS-MU-003", Direction.LONG, 2.5, 2.0, 12_500.0),
        ("POS-AVGO-004", Direction.LONG, 3.0, -2.0, 15_000.0),
        ("POS-JPM-005", Direction.LONG, 3.0, -2.0, 15_000.0),
        ("POS-XOM-006", Direction.LONG, 2.5, 8.0, 12_500.0),
    )
    equity_records = tuple(
        _make_equity_position(
            position_id=position_id,
            direction=direction,
            position_weight_pct=weight,
            unrealized_pnl_pct=pnl_pct,
            delta_adjusted_exposure_usd=dae_usd,
        )
        for position_id, direction, weight, pnl_pct, dae_usd in spec_equity
    )
    options_records = (
        _make_options_position(
            position_id=PositionId("POS-MSFT-007"),
            underlying_ticker=Symbol("MSFT"),
            position_weight_pct=2.8,
            unrealized_pnl_pct=10.0,
            delta=0.45,
        ),
        _make_options_position(
            position_id=PositionId("POS-XLE-008"),
            underlying_ticker=Symbol("XLE"),
            position_weight_pct=1.5,
            unrealized_pnl_pct=-5.0,
            delta=0.60,
        ),
    )
    return tuple(_make_position_view(p) for p in (*equity_records, *options_records))


def _make_full_sector_resolver() -> SectorResolver:
    sector_map = {
        "POS-NVDA-001": "tech",
        "POS-AAPL-002": "tech",
        "POS-MSFT-007": "tech",
        "POS-MU-003": "semis",
        "POS-AVGO-004": "semis",
        "POS-JPM-005": "financials",
        "POS-XOM-006": "energy",
        "POS-XLE-008": "energy",
    }

    def _resolver(position: PositionRecord) -> str | None:
        return sector_map.get(position.position_id)

    return _resolver


def test_render_strategist_header_full_system_profile_full_fixture() -> None:
    positions = _full_system_strategist_positions()
    abandoned_openings = (
        AnalystAbandonedOpening(
            envelope_id="ENV-REC-1",
            direction=Direction.LONG,
            ticker=Symbol("MSFT"),
            instrument_type=InstrumentType.EQUITY,
            size_pct=3.0,
            abandoned_at=datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC),
            failure_reason="broker rejected: stale price",
        ),
    )
    abandoned_actions = (
        StrategistAbandonedAction(
            envelope_id="ENV-SA-1",
            command_type="ADD",
            position_id=PositionId("POS-NVDA-001"),
            order_id=None,
            abandoned_at=datetime(2026, 4, 28, 13, 35, 0, tzinfo=UTC),
            failure_reason="insufficient buying power",
        ),
        StrategistAbandonedAction(
            envelope_id="ENV-SA-2",
            command_type="ADJUST",
            position_id=PositionId("POS-AAPL-002"),
            order_id=None,
            abandoned_at=datetime(2026, 4, 28, 13, 36, 0, tzinfo=UTC),
            failure_reason="market closed",
        ),
        StrategistAbandonedAction(
            envelope_id="ENV-SA-3",
            command_type="CLOSE",
            position_id=PositionId("POS-MU-003"),
            order_id=None,
            abandoned_at=datetime(2026, 4, 28, 13, 37, 0, tzinfo=UTC),
            failure_reason="route timeout",
        ),
        StrategistAbandonedAction(
            envelope_id="ENV-SA-ORD-4",
            command_type="CANCEL",
            position_id=None,
            order_id=OrderId("ORD-9001"),
            abandoned_at=datetime(2026, 4, 28, 13, 38, 0, tzinfo=UTC),
            failure_reason="order already filled",
        ),
    )
    breaches = (
        RegimeTransitionBreach(
            position_id=PositionId("POS-NVDA-001"),
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            current_value=4.2,
            new_limit_value=3.5,
            overage=0.7,
            unit="% of portfolio",
        ),
    )
    drawdown = _make_drawdown(
        intraday_drawdown_pct=2.0,
        current_drawdown_pct=8.5,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.WARNING,
        cumulative_tier=DrawdownTier.CONSTRAINED,
    )
    active = _make_active_parameters(regime_label=RegimeLabel.ELEVATED)
    view = _make_strategist_view(
        positions=positions,
        drawdown=drawdown,
        risk_budget=_full_system_risk_budget(),
        active_risk_parameters=active,
        abandoned_openings=abandoned_openings,
        abandoned_actions=abandoned_actions,
    )
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-002",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
        sector_resolver=_make_full_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        regime_transition_breaches=breaches,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    expected = "\n".join(
        [
            "=== GUARDRAIL STATE (invocation inv-002, 2026-04-28T14:32:05Z) ===",
            "Regime: elevated [unchanged]",
            "",
            "Capital:",
            "  Available for new positions: $300,000 (60.0% of portfolio)",
            "  Per-position max size: $25,000 (5.0% of portfolio, elevated regime)",
            "",
            "Sector headroom (delta-adjusted):",
            "  Tech:       18.3% / 25.0% — room: 6.7% [NORMAL]",
            "  Semis:      12.1% / 25.0% — room: 12.9% [NORMAL]",
            "  Financials: 8.5% / 25.0% — room: 16.5% [NORMAL]",
            "  Energy:     4.0% / 25.0% — room: 21.0% [NORMAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Net short: 10.0% / 30.0% — room: 20.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Options headroom:",
            "  Delta exposure: 22.0% / 40.0% — room: 18.0%",
            "  Theta:          0.1% / 0.2%/day",
            "  Vega:           0.6% / 1.0%/pt",
            "",
            "Position-level constraint proximity:",
            "  POS-NVDA-001:  4.2% of portfolio (max 5.0%) — "
            "P/L: -18.0% of cost (max loss: -30.0%) [⚠ WARNING: size]",
            "  POS-AAPL-002:  3.0% of portfolio (max 5.0%) — P/L: +5.0% of cost (max loss: -30.0%)",
            "  POS-MU-003:    2.5% of portfolio (max 5.0%) — P/L: +2.0% of cost (max loss: -30.0%)",
            "  POS-AVGO-004:  3.0% of portfolio (max 5.0%) — P/L: -2.0% of cost (max loss: -30.0%)",
            "  POS-JPM-005:   3.0% of portfolio (max 5.0%) — P/L: -2.0% of cost (max loss: -30.0%)",
            "  POS-XOM-006:   2.5% of portfolio (max 5.0%) — P/L: +8.0% of cost (max loss: -30.0%)",
            "  POS-MSFT-007:  2.8% of portfolio (max 5.0%) — "
            "P/L: +10.0% of cost (max loss: -80.0%)",
            "  POS-XLE-008:   1.5% of portfolio (max 5.0%) — P/L: -5.0% of cost (max loss: -80.0%)",
            "",
            "Sector exposure breakdown (per position):",
            "  Tech (18.3% / 25.0%):",
            "    POS-NVDA-001: 4.2% (delta-adj)",
            "    POS-AAPL-002: 3.0% (delta-adj)",
            "    POS-MSFT-007: 2.8% (delta-adj) [options, delta 0.45]",
            "  Semis (12.1% / 25.0%):",
            "    POS-MU-003: 2.5% (delta-adj)",
            "    POS-AVGO-004: 3.0% (delta-adj)",
            "  Financials (8.5% / 25.0%):",
            "    POS-JPM-005: 3.0% (delta-adj)",
            "  Energy (4.0% / 25.0%):",
            "    POS-XOM-006: 2.5% (delta-adj)",
            "    POS-XLE-008: 1.5% (delta-adj) [options, delta 0.60]",
            "",
            "Drawdown state:",
            "  Daily:      2.0% / 5.0% [NORMAL]",
            "  Cumulative: 8.5% / 8.0% [⚠ WARNING]",
            "  Cumulative tier: constrained — max position size 3%, "
            "max gross 80%, positions w/ unrealized loss > 10% flagged",
            "",
            "Regime-transition breaches (if any):",
            "  POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%",
            "",
            "Abandoned openings from prior invocation "
            "(portfolio awareness; analyst owns re-evaluation):",
            "  ENV-REC-1  long MSFT equity  3.0%  — "
            "abandoned at 2026-04-28T13:30:00Z (broker rejected: stale price)",
            "",
            "Abandoned position actions from prior invocation "
            "(decide on current grounds whether to re-propose):",
            "  ENV-SA-1: ADD on POS-NVDA-001 — "
            "abandoned at 2026-04-28T13:35:00Z (insufficient buying power)",
            "  ENV-SA-2: ADJUST on POS-AAPL-002 — "
            "abandoned at 2026-04-28T13:36:00Z (market closed)",
            "  ENV-SA-3: CLOSE on POS-MU-003 — abandoned at 2026-04-28T13:37:00Z (route timeout)",
            "  ENV-SA-ORD-4: CANCEL on ORD-9001 — "
            "abandoned at 2026-04-28T13:38:00Z (order already filled)",
            "===",
        ]
    )
    assert rendered == expected


# ---------------------------------------------------------------------------
# Micro happy path
# ---------------------------------------------------------------------------


def test_render_strategist_header_micro_profile_full_fixture() -> None:
    positions = tuple(
        _make_position_view(
            _make_equity_position(
                position_id=position_id,
                position_weight_pct=weight,
                unrealized_pnl_pct=pnl_pct,
                delta_adjusted_exposure_usd=weight * 5_000.0,
            )
        )
        for position_id, weight, pnl_pct in (
            ("POS-NVDA-001", 3.0, -2.0),
            ("POS-AMD-002", 2.5, 4.0),
            ("POS-AAPL-003", 2.5, 1.0),
            ("POS-MU-004", 2.0, -1.0),
            ("POS-AVGO-005", 1.5, 3.0),
        )
    )
    view = _make_strategist_view(
        positions=positions,
        risk_budget=_micro_risk_budget(),
        active_risk_parameters=_make_active_parameters(
            include_max_loss_options=False,
        ),
    )
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
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
            "  Semis: 12.1% / 25.0% — room: 12.9% [NORMAL]",
            "",
            "Directional headroom:",
            "  Net long:  42.0% / 60.0% — room: 18.0%",
            "  Gross:     78.0% / 120.0% — room: 42.0%",
            "",
            "Position-level constraint proximity:",
            "  POS-NVDA-001:  3.0% of portfolio (max 5.0%) — P/L: -2.0% of cost (max loss: -30.0%)",
            "  POS-AMD-002:   2.5% of portfolio (max 5.0%) — P/L: +4.0% of cost (max loss: -30.0%)",
            "  POS-AAPL-003:  2.5% of portfolio (max 5.0%) — P/L: +1.0% of cost (max loss: -30.0%)",
            "  POS-MU-004:    2.0% of portfolio (max 5.0%) — P/L: -1.0% of cost (max loss: -30.0%)",
            "  POS-AVGO-005:  1.5% of portfolio (max 5.0%) — P/L: +3.0% of cost (max loss: -30.0%)",
            "",
            "Sector exposure breakdown (per position):",
            "  Tech (18.3% / 25.0%):",
            "    POS-NVDA-001: 3.0% (delta-adj)",
            "    POS-AMD-002: 2.5% (delta-adj)",
            "    POS-AAPL-003: 2.5% (delta-adj)",
            "  Semis (12.1% / 25.0%):",
            "    POS-MU-004: 2.0% (delta-adj)",
            "    POS-AVGO-005: 1.5% (delta-adj)",
            "",
            "Drawdown state:",
            "  Daily:      1.5% / 5.0% [NORMAL]",
            "  Cumulative: 3.0% / 8.0% [NORMAL]",
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
            "  Options: DISABLED for this portfolio",
            "  Short selling: DISABLED for this portfolio",
            "===",
        ]
    )
    assert rendered == expected


# ---------------------------------------------------------------------------
# Empty positions
# ---------------------------------------------------------------------------


def test_render_strategist_header_renders_none_for_empty_positions() -> None:
    view = _make_strategist_view(positions=())
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    proximity_idx = lines.index("Position-level constraint proximity:")
    assert lines[proximity_idx + 1] == "  None"
    # Each sector group emits its header followed by `(no positions)`.
    breakdown_idx = lines.index("Sector exposure breakdown (per position):")
    assert lines[breakdown_idx + 1] == "  Tech (18.3% / 25.0%):"
    assert lines[breakdown_idx + 2] == "    (no positions)"
    assert lines[breakdown_idx + 3] == "  Semis (12.1% / 25.0%):"
    assert lines[breakdown_idx + 4] == "    (no positions)"


# ---------------------------------------------------------------------------
# Cumulative tier rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "expected_label", "expected_restrictions"),
    [
        (
            DrawdownTier.CONSTRAINED,
            "constrained",
            "max position size 3%, max gross 80%, positions w/ unrealized loss > 10% flagged",
        ),
        (
            DrawdownTier.HEAVILY_CONSTRAINED,
            "heavily constrained",
            "max position size 2%, max gross 60%, positions w/ unrealized loss > 15% flagged",
        ),
        (
            DrawdownTier.FULL_HALT,
            "full halt",
            "no new positions; orderly reductions only",
        ),
    ],
)
def test_render_strategist_header_renders_cumulative_tier_line(
    tier: DrawdownTier,
    expected_label: str,
    expected_restrictions: str,
) -> None:
    drawdown = _make_drawdown(cumulative_tier=tier)
    view = _make_strategist_view(drawdown=drawdown)
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    expected_line = f"  Cumulative tier: {expected_label} — {expected_restrictions}"
    assert expected_line in rendered.splitlines()


# ---------------------------------------------------------------------------
# Sector breakdown unclassified
# ---------------------------------------------------------------------------


def test_render_strategist_header_groups_unclassified_position_at_end() -> None:
    classified = _make_equity_position(
        position_id=PositionId("POS-NVDA-001"),
        position_weight_pct=2.0,
        unrealized_pnl_pct=1.0,
    )
    unclassified = _make_equity_position(
        position_id=PositionId("POS-XYZ-099"),
        position_weight_pct=1.5,
        unrealized_pnl_pct=2.0,
    )

    def _resolver(position: PositionRecord) -> str | None:
        if position.position_id == "POS-NVDA-001":
            return "tech"
        return None

    view = _make_strategist_view(
        positions=(_make_position_view(classified), _make_position_view(unclassified)),
    )
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_resolver,
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    breakdown_idx = lines.index("Sector exposure breakdown (per position):")
    # Unclassified group sits after the active-sector groups.
    unclassified_idx = lines.index("  Unclassified:")
    assert unclassified_idx > breakdown_idx
    assert lines[unclassified_idx + 1] == "    POS-XYZ-099: 1.5% (delta-adj)"


# ---------------------------------------------------------------------------
# Position zone tag thresholds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("weight", "expected_tag"),
    [
        (3.4, ""),  # 68% — NORMAL, no tag
        (3.5, " [⚠ WARNING: size]"),  # 70% — WARNING
        (4.2, " [⚠ WARNING: size]"),  # 84% — WARNING
        (4.3, " [\U0001f534 CRITICAL: size]"),  # 86% — CRITICAL
        (4.7, " [\U0001f534 CRITICAL: size]"),  # 94% — CRITICAL
        (4.75, " [BLOCKED: size]"),  # 95% — BLOCKED
        (5.0, " [BLOCKED: size]"),  # 100% — BLOCKED
    ],
)
def test_render_strategist_header_emits_zone_tag_per_threshold(
    weight: float, expected_tag: str
) -> None:
    pos = _make_equity_position(
        position_id=PositionId("POS-NVDA-001"),
        position_weight_pct=weight,
    )
    view = _make_strategist_view(positions=(_make_position_view(pos),))
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    proximity_line = next(
        line for line in rendered.splitlines() if line.lstrip().startswith("POS-NVDA-001:")
    )
    assert proximity_line.endswith(f"of cost (max loss: -30.0%){expected_tag}")


def test_render_strategist_header_critical_from_size_not_positive_pnl() -> None:
    """ALP-580 regression: debug-pos-07 (weight 4.5%, P/L +19900%) in the strategist header.

    The CRITICAL tag must attribute to size proximity (4.5% / 5.0% = 90%), not
    to the +19900% P/L — a large gain is the opposite of a max-loss breach.
    """
    pos = _make_equity_position(
        position_id="POS-NVDA-001",
        position_weight_pct=4.5,
        unrealized_pnl_pct=19_900.0,
    )
    view = _make_strategist_view(positions=(_make_position_view(pos),))
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    proximity_line = next(
        line for line in rendered.splitlines() if line.lstrip().startswith("POS-NVDA-001:")
    )
    # An exact `: size]` tag rules out the `: loss]` and `: size+loss]` variants.
    assert proximity_line.endswith("[\U0001f534 CRITICAL: size]")


# ---------------------------------------------------------------------------
# Drawdown source field routing
# ---------------------------------------------------------------------------


def test_render_strategist_header_reads_intraday_for_daily_and_current_for_cumulative() -> None:
    drawdown = _make_drawdown(intraday_drawdown_pct=2.5, current_drawdown_pct=4.0)
    view = _make_strategist_view(drawdown=drawdown)
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    daily_idx = lines.index("Drawdown state:") + 1
    assert lines[daily_idx].startswith("  Daily:      2.5%")
    assert lines[daily_idx + 1].startswith("  Cumulative: 4.0%")


# ---------------------------------------------------------------------------
# Regime-transition breach block omission and row formats
# ---------------------------------------------------------------------------


def test_render_strategist_header_omits_regime_transition_block_when_empty() -> None:
    view = _make_strategist_view()
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        regime_transition_breaches=(),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert "Regime-transition breaches" not in rendered
    assert "(none)" not in rendered
    # Blank-line discipline still holds.
    for prev, nxt in pairwise(rendered.splitlines()):
        assert not (prev == "" and nxt == ""), "double blank line found"


def test_render_strategist_header_renders_per_position_max_size_breach_row() -> None:
    breach = RegimeTransitionBreach(
        position_id=PositionId("POS-NVDA-001"),
        rule_id="position_max_size_pct",
        rule_label="Per-position max size",
        current_value=4.2,
        new_limit_value=3.5,
        overage=0.7,
        unit="% of portfolio",
    )
    active = _make_active_parameters(regime_label=RegimeLabel.ELEVATED)
    view = _make_strategist_view(active_risk_parameters=active)
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        regime_transition_breaches=(breach,),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert (
        "  POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%"
        in rendered.splitlines()
    )


def test_render_strategist_header_renders_aggregate_breach_row() -> None:
    breach = RegimeTransitionBreach(
        position_id=None,
        rule_id="sector_concentration_tech",
        rule_label="Sector concentration (Tech)",
        current_value=28.0,
        new_limit_value=20.0,
        overage=8.0,
        unit="% of portfolio",
    )
    active = _make_active_parameters(regime_label=RegimeLabel.ELEVATED)
    view = _make_strategist_view(active_risk_parameters=active)
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        regime_transition_breaches=(breach,),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert (
        "  Sector concentration (Tech): 28.0% exceeds elevated regime limit of "
        "20.0% — overage 8.0%" in rendered.splitlines()
    )


def test_render_strategist_header_renders_per_position_non_max_size_with_label_suffix() -> None:
    breach = RegimeTransitionBreach(
        position_id=PositionId("POS-AMD-002"),
        rule_id="single_short_max_pct",
        rule_label="Single short max size",
        current_value=3.2,
        new_limit_value=3.0,
        overage=0.2,
        unit="% of portfolio",
    )
    active = _make_active_parameters(regime_label=RegimeLabel.ELEVATED)
    view = _make_strategist_view(active_risk_parameters=active)
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=True,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        regime_transition_breaches=(breach,),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert any(
        "POS-AMD-002: 3.2% exceeds elevated regime limit of 3.0% — overage 0.2% "
        "[Single short max size]" in line
        for line in rendered.splitlines()
    )


def test_render_strategist_header_renders_mixed_breach_rows_in_order() -> None:
    per_pos = RegimeTransitionBreach(
        position_id=PositionId("POS-NVDA-001"),
        rule_id="position_max_size_pct",
        rule_label="Per-position max size",
        current_value=4.2,
        new_limit_value=3.5,
        overage=0.7,
        unit="% of portfolio",
    )
    aggregate = RegimeTransitionBreach(
        position_id=None,
        rule_id="sector_concentration_tech",
        rule_label="Sector concentration (Tech)",
        current_value=28.0,
        new_limit_value=20.0,
        overage=8.0,
        unit="% of portfolio",
    )
    active = _make_active_parameters(regime_label=RegimeLabel.ELEVATED)
    view = _make_strategist_view(active_risk_parameters=active)
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        regime_transition_breaches=(per_pos, aggregate),
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    lines = rendered.splitlines()
    header_idx = lines.index("Regime-transition breaches (if any):")
    # Two breach rows render in input order with no spurious blank line between them.
    assert lines[header_idx + 1].startswith("  POS-NVDA-001:")
    assert lines[header_idx + 2].startswith("  Sector concentration (Tech):")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_render_strategist_header_is_deterministic() -> None:
    positions = _full_system_strategist_positions()
    breaches = (
        RegimeTransitionBreach(
            position_id=PositionId("POS-NVDA-001"),
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            current_value=4.2,
            new_limit_value=3.5,
            overage=0.7,
            unit="% of portfolio",
        ),
    )
    view = _make_strategist_view(
        positions=positions,
        risk_budget=_full_system_risk_budget(),
        active_risk_parameters=_make_active_parameters(regime_label=RegimeLabel.ELEVATED),
    )
    kwargs = dict(
        strategist_view=view,
        invocation_id="inv-002",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
        sector_resolver=_make_full_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        regime_transition_breaches=breaches,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    first = render_strategist_header(**kwargs)  # type: ignore[arg-type]
    second = render_strategist_header(**kwargs)  # type: ignore[arg-type]
    assert first == second


# ---------------------------------------------------------------------------
# Feature-flag closure invariants
# ---------------------------------------------------------------------------


def test_render_strategist_header_raises_when_options_disabled_but_options_rule_present() -> None:
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
    view = _make_strategist_view(risk_budget=risk_budget)
    with pytest.raises(ValueError, match="options_delta_pct"):
        render_strategist_header(
            strategist_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_micro_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_strategist_header_raises_when_short_selling_disabled_but_net_short_present() -> (
    None
):
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
    view = _make_strategist_view(risk_budget=risk_budget)
    with pytest.raises(ValueError, match="net_short_pct"):
        render_strategist_header(
            strategist_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_micro_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_strategist_header_raises_when_options_disabled_but_options_position_present() -> (
    None
):
    options_pos = _make_options_position(
        position_id=PositionId("POS-MSFT-001"),
        underlying_ticker=Symbol("MSFT"),
        position_weight_pct=2.0,
    )
    view = _make_strategist_view(positions=(_make_position_view(options_pos),))
    with pytest.raises(ValueError, match="POS-MSFT-001"):
        render_strategist_header(
            strategist_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_micro_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


# ---------------------------------------------------------------------------
# Missing required rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_id",
    [
        "net_long_pct",
        "gross_exposure_pct",
        "sector_concentration_tech",
    ],
)
def test_render_strategist_header_raises_when_risk_budget_rule_missing(rule_id: str) -> None:
    risk_budget = RiskBudgetConsumption(
        entries=tuple(entry for entry in _micro_risk_budget().entries if entry.rule_id != rule_id)
    )
    view = _make_strategist_view(risk_budget=risk_budget)
    with pytest.raises(ValueError, match=rule_id):
        render_strategist_header(
            strategist_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_micro_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


@pytest.mark.parametrize(
    "rule_id",
    [
        "position_max_size_pct",
        "daily_drawdown_pct",
        "cumulative_drawdown_pct",
    ],
)
def test_render_strategist_header_raises_when_active_parameter_missing(
    rule_id: str,
) -> None:
    base = _make_active_parameters()
    pruned = ActiveRiskParameterSet(
        regime_label=base.regime_label,
        transition_state=base.transition_state,
        transition_invocations_remaining=base.transition_invocations_remaining,
        parameter_change_flag=base.parameter_change_flag,
        entries=tuple(entry for entry in base.entries if entry.rule_id != rule_id),
        active_overlays=base.active_overlays,
    )
    view = _make_strategist_view(active_risk_parameters=pruned)
    with pytest.raises(ValueError, match=rule_id):
        render_strategist_header(
            strategist_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_micro_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


# ---------------------------------------------------------------------------
# Abandoned action command_type routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command_type", ["ADD", "ADJUST", "CLOSE"])
def test_render_strategist_header_renders_position_action_against_position_id(
    command_type: str,
) -> None:
    action = StrategistAbandonedAction(
        envelope_id=f"ENV-SA-{command_type}",
        command_type=command_type,  # type: ignore[arg-type]
        position_id=PositionId("POS-NVDA-001"),
        order_id=None,
        abandoned_at=datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC),
        failure_reason="reason",
    )
    view = _make_strategist_view(abandoned_actions=(action,))
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert (
        f"  ENV-SA-{command_type}: {command_type} on POS-NVDA-001 — "
        f"abandoned at 2026-04-28T13:30:00Z (reason)" in rendered.splitlines()
    )


def test_render_strategist_header_renders_cancel_action_against_order_id() -> None:
    action = StrategistAbandonedAction(
        envelope_id="ENV-SA-ORD-1",
        command_type="CANCEL",
        position_id=None,
        order_id=OrderId("ORD-9001"),
        abandoned_at=datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC),
        failure_reason="reason",
    )
    view = _make_strategist_view(abandoned_actions=(action,))
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert (
        "  ENV-SA-ORD-1: CANCEL on ORD-9001 — "
        "abandoned at 2026-04-28T13:30:00Z (reason)" in rendered.splitlines()
    )


def test_render_strategist_header_raises_when_position_action_missing_position_id() -> None:
    action = StrategistAbandonedAction(
        envelope_id="ENV-SA-1",
        command_type="ADD",
        position_id=None,
        order_id=None,
        abandoned_at=datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC),
        failure_reason="reason",
    )
    view = _make_strategist_view(abandoned_actions=(action,))
    with pytest.raises(ValueError, match="ENV-SA-1"):
        render_strategist_header(
            strategist_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_micro_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


def test_render_strategist_header_raises_when_cancel_action_missing_order_id() -> None:
    action = StrategistAbandonedAction(
        envelope_id="ENV-SA-ORD-1",
        command_type="CANCEL",
        position_id=None,
        order_id=None,
        abandoned_at=datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC),
        failure_reason="reason",
    )
    view = _make_strategist_view(abandoned_actions=(action,))
    with pytest.raises(ValueError, match="ENV-SA-ORD-1"):
        render_strategist_header(
            strategist_view=view,
            invocation_id="inv-001",
            timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
            options_enabled=False,
            short_selling_enabled=False,
            active_sectors=("tech", "semis"),
            config=_make_state_delivery_config(),
            sector_label_display=_MICRO_SECTOR_LABELS,
            sector_resolver=_make_micro_sector_resolver(),
            total_portfolio_value_usd=500_000.0,
            available_for_new_positions_usd=300_000.0,
            position_zones=_DEFAULT_POSITION_ZONES,
        )


# ---------------------------------------------------------------------------
# Blank-line discipline
# ---------------------------------------------------------------------------


def test_render_strategist_header_has_no_double_blank_lines() -> None:
    positions = _full_system_strategist_positions()
    view = _make_strategist_view(
        positions=positions,
        risk_budget=_full_system_risk_budget(),
        active_risk_parameters=_make_active_parameters(regime_label=RegimeLabel.ELEVATED),
    )
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-002",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
        sector_resolver=_make_full_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    for prev, nxt in pairwise(rendered.splitlines()):
        assert not (prev == "" and nxt == ""), "double blank line found"


def test_render_strategist_header_has_no_trailing_blank_line() -> None:
    view = _make_strategist_view()
    rendered = render_strategist_header(
        strategist_view=view,
        invocation_id="inv-001",
        timestamp=datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC),
        options_enabled=False,
        short_selling_enabled=False,
        active_sectors=("tech", "semis"),
        config=_make_state_delivery_config(),
        sector_label_display=_MICRO_SECTOR_LABELS,
        sector_resolver=_make_micro_sector_resolver(),
        total_portfolio_value_usd=500_000.0,
        available_for_new_positions_usd=300_000.0,
        position_zones=_DEFAULT_POSITION_ZONES,
    )
    assert not rendered.endswith("\n")
    assert rendered.splitlines()[-1] == "==="
