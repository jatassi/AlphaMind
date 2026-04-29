"""Tests for exposure rule specs (story 04).

Each exposure rule's ``read_current`` and ``contribute`` is unit-tested
against synthetic state and proposals, asserting the documented arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureFlagsView,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleSpec,
    build_active_specs,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _config(
    *,
    effective_limits: Mapping[str, float] | None = None,
    active_sectors: tuple[str, ...] = ("tech", "semis"),
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
) -> LibraryConfig:
    if effective_limits is None:
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
    escalation_zones = MappingProxyType({key: _zones() for key in effective_limits})
    return LibraryConfig(
        effective_limits=MappingProxyType(dict(effective_limits)),
        escalation_zones=escalation_zones,
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=active_sectors,
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _snapshot(
    *,
    portfolio_value_usd: float = 100_000.0,
    sector_exposure_pct: Mapping[str, float] | None = None,
    net_long_pct: float = 40.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 50.0,
    position_max_size_pct: float = 5.0,
    single_short_max_pct: float = 0.0,
    total_short_pct: float = 0.0,
    daily_borrow_cost_pct: float = 0.0,
    options_delta_pct: float = 0.0,
    portfolio_theta_pct_per_day: float = 0.0,
    portfolio_vega_pct_per_iv_point: float = 0.0,
    cash_usd: float = 20_000.0,
    reserved_for_pending_orders_usd: float = 0.0,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {"tech": 18.0, "semis": 12.0}
    if existing_positions is None:
        existing_positions = {}
    return PortfolioStateSnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=reserved_for_pending_orders_usd,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct)),
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        gross_pct=gross_pct,
        options_delta_pct=options_delta_pct,
        portfolio_theta_pct_per_day=portfolio_theta_pct_per_day,
        portfolio_vega_pct_per_iv_point=portfolio_vega_pct_per_iv_point,
        total_short_pct=total_short_pct,
        single_short_max_pct=single_short_max_pct,
        daily_borrow_cost_pct=daily_borrow_cost_pct,
        position_max_size_pct=position_max_size_pct,
        existing_positions=MappingProxyType(dict(existing_positions)),
    )


def _proposal(
    *,
    proposal_id: str = "P-1",
    sector: str = "tech",
    direction: Direction = Direction.LONG,
    asset_type: AssetType = AssetType.EQUITY,
    notional_usd: float = 5_000.0,
    quantity: float = 50.0,
    action: Action = Action.OPEN,
    existing_position_id: str | None = None,
    daily_borrow_cost_usd: float | None = None,
    reserves_capital: bool = False,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying="ABC",
        sector=sector,
        direction=direction,
        asset_type=asset_type,
        notional_usd=notional_usd,
        quantity=quantity,
        option_legs=None,
        action=action,
        existing_position_id=existing_position_id,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital=reserves_capital,
    )


def _dae(*, signed_notional_usd: float = 5_000.0) -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id="P-1",
        signed_notional_usd=signed_notional_usd,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


def _existing(
    *,
    position_id: str = "POS-1",
    direction: Direction = Direction.LONG,
    asset_type: AssetType = AssetType.EQUITY,
    notional_usd: float = 8_000.0,
    daily_borrow_cost_usd: float | None = None,
    reserves_capital_usd: float = 0.0,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=position_id,
        underlying="ABC",
        sector="tech",
        direction=direction,
        asset_type=asset_type,
        notional_usd=notional_usd,
        delta_adjusted_exposure_usd=notional_usd,
        current_greeks=None,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital_usd=reserves_capital_usd,
    )


def _spec_by_id(specs: tuple[RuleSpec, ...], rule_id: str) -> RuleSpec:
    for spec in specs:
        if spec.rule_id == rule_id:
            return spec
    raise KeyError(rule_id)


# ---------------------------------------------------------------------------
# position_max_size_pct
# ---------------------------------------------------------------------------


def test_position_max_size_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(position_max_size_pct=7.5)
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.read_current(state, config) == 7.5


def test_position_max_size_contribute_zero_when_proposal_smaller_than_current_max() -> None:
    """An OPEN smaller than current max contributes 0."""
    config = _config()
    state = _snapshot(position_max_size_pct=8.0, portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    proposal = _proposal(action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    # proposal_size_pct = 5%; current max = 8% → contribution 0
    assert spec.contribute(proposal, dae, state, config) == 0.0


def test_position_max_size_contribute_delta_when_proposal_larger() -> None:
    """An OPEN larger than current max contributes (proposal_pct - current_max)."""
    config = _config()
    state = _snapshot(position_max_size_pct=5.0, portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    proposal = _proposal(action=Action.OPEN, notional_usd=8_000.0)
    dae = _dae(signed_notional_usd=8_000.0)
    # proposal_size_pct = 8%; current max = 5% → contribution 3
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(3.0)


def test_position_max_size_contribute_zero_for_close() -> None:
    """CLOSE never increases max."""
    config = _config()
    state = _snapshot(position_max_size_pct=8.0)
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    proposal = _proposal(action=Action.CLOSE, notional_usd=10_000.0)
    dae = _dae(signed_notional_usd=-10_000.0)
    assert spec.contribute(proposal, dae, state, config) == 0.0


# ---------------------------------------------------------------------------
# sector_concentration_{sector}
# ---------------------------------------------------------------------------


def test_sector_concentration_read_current_uses_sector_key() -> None:
    config = _config(active_sectors=("tech", "semis"))
    state = _snapshot(sector_exposure_pct={"tech": 20.0, "semis": 10.0})
    specs = build_active_specs(config)
    tech_spec = _spec_by_id(specs, "sector_concentration_tech")
    semis_spec = _spec_by_id(specs, "sector_concentration_semis")
    assert tech_spec.read_current(state, config) == 20.0
    assert semis_spec.read_current(state, config) == 10.0


def test_sector_concentration_contribute_signed_for_matching_sector() -> None:
    config = _config(active_sectors=("tech",))
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(sector="tech", action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(5.0)


def test_sector_concentration_contribute_zero_for_other_sector() -> None:
    config = _config(active_sectors=("tech",))
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(sector="energy", action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(proposal, dae, state, config) == 0.0


def test_sector_concentration_contribute_negative_for_close() -> None:
    """CLOSE in the sector reduces concentration via signed_notional_usd<0."""
    config = _config(active_sectors=("tech",))
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(
        sector="tech",
        action=Action.CLOSE,
        notional_usd=5_000.0,
        existing_position_id="POS-1",
    )
    dae = _dae(signed_notional_usd=-5_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-5.0)


# ---------------------------------------------------------------------------
# net_long_pct
# ---------------------------------------------------------------------------


def test_net_long_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(net_long_pct=42.5)
    spec = _spec_by_id(build_active_specs(config), "net_long_pct")
    assert spec.read_current(state, config) == 42.5


def test_net_long_contribute_signed() -> None:
    """Long OPEN contributes positive; short OPEN contributes negative."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "net_long_pct")

    long_open = _proposal(direction=Direction.LONG, action=Action.OPEN)
    long_dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(long_open, long_dae, state, config) == pytest.approx(5.0)

    short_open = _proposal(direction=Direction.SHORT, action=Action.OPEN)
    short_dae = _dae(signed_notional_usd=-5_000.0)
    assert spec.contribute(short_open, short_dae, state, config) == pytest.approx(-5.0)


# ---------------------------------------------------------------------------
# net_short_pct
# ---------------------------------------------------------------------------


def test_net_short_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(net_short_pct=15.0)
    spec = _spec_by_id(build_active_specs(config), "net_short_pct")
    assert spec.read_current(state, config) == 15.0


def test_net_short_contribute_flipped_sign() -> None:
    """Short OPEN (signed_notional<0) contributes positively to net short."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "net_short_pct")

    short_open = _proposal(direction=Direction.SHORT, action=Action.OPEN)
    short_dae = _dae(signed_notional_usd=-5_000.0)
    # -(-5_000) / 100_000 * 100 = +5
    assert spec.contribute(short_open, short_dae, state, config) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# gross_exposure_pct
# ---------------------------------------------------------------------------


def test_gross_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(gross_pct=68.0)
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    assert spec.read_current(state, config) == 68.0


def test_gross_contribute_open_uses_absolute_signed_notional() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(5.0)


def test_gross_contribute_close_long_decreases_gross() -> None:
    """CLOSE-LONG reduces gross by the existing position's notional."""
    config = _config()
    existing = _existing(direction=Direction.LONG, notional_usd=8_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(
        direction=Direction.LONG,
        action=Action.CLOSE,
        notional_usd=8_000.0,
        existing_position_id="POS-1",
    )
    dae = _dae(signed_notional_usd=-8_000.0)
    # Contribution = -existing.notional / portfolio * 100 = -8
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-8.0)


def test_gross_contribute_close_short_decreases_gross() -> None:
    """CLOSE-SHORT also reduces gross."""
    config = _config()
    existing = _existing(direction=Direction.SHORT, notional_usd=4_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-2": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(
        direction=Direction.SHORT,
        action=Action.CLOSE,
        notional_usd=4_000.0,
        existing_position_id="POS-2",
    )
    dae = _dae(signed_notional_usd=4_000.0)  # short close → +signed_notional
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-4.0)


def test_gross_contribute_add_increases_gross_by_signed_magnitude() -> None:
    config = _config()
    existing = _existing(direction=Direction.LONG, notional_usd=5_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(action=Action.ADD, existing_position_id="POS-1")
    dae = _dae(signed_notional_usd=2_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(2.0)


def test_gross_contribute_zero_for_adjust_and_cancel() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    for action in (Action.ADJUST, Action.CANCEL):
        proposal = _proposal(action=action)
        dae = _dae(signed_notional_usd=0.0)
        assert spec.contribute(proposal, dae, state, config) == 0.0
