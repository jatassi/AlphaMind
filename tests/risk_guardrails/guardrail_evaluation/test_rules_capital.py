"""Tests for capital rule specs (story 04).

Two rules in this category — ``min_cash_reserve_pct`` (inverse) and
``pending_order_capital_pct`` (standard).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.money import money
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


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _config() -> LibraryConfig:
    keys = ("min_cash_reserve_pct", "pending_order_capital_pct")
    return LibraryConfig(
        effective_limits=MappingProxyType({k: 10.0 for k in keys}),
        escalation_zones=MappingProxyType({k: _zones() for k in keys}),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech",),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _snapshot(
    *,
    portfolio_value_usd: float = 100_000.0,
    cash_usd: float = 20_000.0,
    reserved_for_pending_orders_usd: float = 0.0,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if existing_positions is None:
        existing_positions = {}
    return PortfolioStateSnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=reserved_for_pending_orders_usd,
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
        existing_positions=MappingProxyType(dict(existing_positions)),
    )


def _proposal(
    *,
    proposal_id: str = "P-1",
    direction: Direction = Direction.LONG,
    asset_type: AssetType = AssetType.EQUITY,
    notional_usd: float = 5_000.0,
    action: Action = Action.OPEN,
    existing_position_id: str | None = None,
    reserves_capital: bool = False,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=Symbol("ABC"),
        sector="tech",
        direction=direction,
        asset_type=asset_type,
        notional_usd=money(notional_usd),
        quantity=50.0,
        option_legs=None,
        action=action,
        existing_position_id=existing_position_id,
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


def _spec_by_id(specs: tuple[RuleSpec, ...], rule_id: str) -> RuleSpec:
    for spec in specs:
        if spec.rule_id == rule_id:
            return spec
    raise KeyError(rule_id)


# ---------------------------------------------------------------------------
# min_cash_reserve_pct
# ---------------------------------------------------------------------------


def test_min_cash_reserve_read_current_returns_cash_pct() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0, cash_usd=15_000.0)
    spec = _spec_by_id(build_active_specs(config), "min_cash_reserve_pct")
    assert spec.read_current(state, config) == pytest.approx(15.0)


def test_min_cash_reserve_long_open_reduces_cash() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "min_cash_reserve_pct")
    proposal = _proposal(
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        action=Action.OPEN,
        notional_usd=5_000.0,
    )
    # cash impact = -5_000; pct = -5
    assert spec.contribute(proposal, _dae(), state, config) == pytest.approx(-5.0)


def test_min_cash_reserve_option_open_reduces_cash() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "min_cash_reserve_pct")
    proposal = _proposal(
        direction=Direction.LONG,
        asset_type=AssetType.OPTION,
        action=Action.OPEN,
        notional_usd=2_000.0,
    )
    assert spec.contribute(proposal, _dae(), state, config) == pytest.approx(-2.0)


def test_min_cash_reserve_short_equity_open_does_not_reduce_cash() -> None:
    """SHORT equity OPEN is Reg-T-margin handled separately; cash unchanged."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "min_cash_reserve_pct")
    proposal = _proposal(
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        action=Action.OPEN,
        notional_usd=4_000.0,
    )
    assert spec.contribute(proposal, _dae(signed_notional_usd=-4_000.0), state, config) == 0.0


def test_min_cash_reserve_close_releases_cash() -> None:
    config = _config()
    existing = ExistingPosition(
        position_id=PositionId("POS-1"),
        underlying=Symbol("ABC"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=8_000.0,
        delta_adjusted_exposure_usd=8_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "min_cash_reserve_pct")
    proposal = _proposal(action=Action.CLOSE, existing_position_id="POS-1")
    # cash impact = +8_000; pct = +8
    assert spec.contribute(proposal, _dae(), state, config) == pytest.approx(8.0)


def test_min_cash_reserve_uses_inverse_flag() -> None:
    config = _config()
    spec = _spec_by_id(build_active_specs(config), "min_cash_reserve_pct")
    assert spec.inverse is True


# ---------------------------------------------------------------------------
# pending_order_capital_pct
# ---------------------------------------------------------------------------


def test_pending_order_capital_read_current_uses_reserved_field() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0, reserved_for_pending_orders_usd=4_000.0)
    spec = _spec_by_id(build_active_specs(config), "pending_order_capital_pct")
    assert spec.read_current(state, config) == pytest.approx(4.0)


def test_pending_order_capital_open_with_reserves_capital_flag() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "pending_order_capital_pct")
    proposal = _proposal(action=Action.OPEN, notional_usd=5_000.0, reserves_capital=True)
    assert spec.contribute(proposal, _dae(), state, config) == pytest.approx(5.0)


def test_pending_order_capital_open_without_reserves_capital_flag() -> None:
    """Marketable orders set reserves_capital=False → contribute 0."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "pending_order_capital_pct")
    proposal = _proposal(action=Action.OPEN, notional_usd=5_000.0, reserves_capital=False)
    assert spec.contribute(proposal, _dae(), state, config) == 0.0


def test_pending_order_capital_cancel_releases_reservation() -> None:
    config = _config()
    existing = ExistingPosition(
        position_id=PositionId("POS-1"),
        underlying=Symbol("ABC"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=0.0,
        delta_adjusted_exposure_usd=0.0,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=3_000.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "pending_order_capital_pct")
    proposal = _proposal(action=Action.CANCEL, existing_position_id="POS-1")
    assert spec.contribute(proposal, _dae(), state, config) == pytest.approx(-3.0)


def test_pending_order_capital_uses_normal_classification() -> None:
    """``pending_order_capital_pct`` is *not* inverse — it's a max."""
    config = _config()
    spec = _spec_by_id(build_active_specs(config), "pending_order_capital_pct")
    assert spec.inverse is False
