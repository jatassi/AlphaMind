"""Tests for options-greeks rule specs (story 04).

The three rules in this category — ``options_delta_pct``,
``portfolio_theta_pct_per_day``, ``portfolio_vega_pct_per_iv_point`` — are all
gated on ``feature_flags.options_enabled=True``. Theta and vega use the
``magnitude=True`` flag on their ``RuleSpec``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureFlagsView,
    Greeks,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleSpec,
    build_active_specs,
)


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _config(*, options_enabled: bool = True) -> LibraryConfig:
    keys = (
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
    )
    return LibraryConfig(
        effective_limits=MappingProxyType({k: 1.0 for k in keys}),
        escalation_zones=MappingProxyType({k: _zones() for k in keys}),
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=True,
        ),
        active_sectors=("tech",),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _snapshot(
    *,
    portfolio_value_usd: float = 100_000.0,
    options_delta_pct: float = 3.0,
    portfolio_theta_pct_per_day: float = -0.05,
    portfolio_vega_pct_per_iv_point: float = 0.10,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if existing_positions is None:
        existing_positions = {}
    return PortfolioStateSnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=20_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": 0.0}),
        net_long_pct=0.0,
        net_short_pct=0.0,
        gross_pct=0.0,
        options_delta_pct=options_delta_pct,
        portfolio_theta_pct_per_day=portfolio_theta_pct_per_day,
        portfolio_vega_pct_per_iv_point=portfolio_vega_pct_per_iv_point,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=0.0,
        existing_positions=MappingProxyType(dict(existing_positions)),
    )


def _option_proposal(
    *,
    proposal_id: str = "P-OPT",
    quantity: float = 10.0,
    action: Action = Action.OPEN,
    asset_type: AssetType = AssetType.OPTION,
    existing_position_id: str | None = None,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=asset_type,
        notional_usd=2_000.0,
        quantity=quantity,
        option_legs=None,
        action=action,
        existing_position_id=existing_position_id,
    )


def _equity_proposal() -> ProposedDelta:
    return ProposedDelta(
        id="P-EQ",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=5_000.0,
        quantity=50.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
    )


def _option_dae(
    *,
    signed_notional_usd: float = 4_500.0,
    delta: float = 0.45,
    gamma: float = 0.02,
    theta: float = -0.10,
    vega: float = 0.20,
) -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id="P-OPT",
        signed_notional_usd=signed_notional_usd,
        net_greeks=Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega),
        iv_used=0.30,
        iv_source=None,
        unbuffered_delta=0.40,
    )


def _existing_option_position(
    *,
    position_id: str = "POS-OPT",
    quantity: float = 10.0,
    theta: float = -0.10,
    vega: float = 0.20,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=position_id,
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.OPTION,
        notional_usd=2_000.0,
        delta_adjusted_exposure_usd=4_500.0,
        current_greeks=Greeks(delta=0.45, gamma=0.02, theta=theta, vega=vega),
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
        quantity=quantity,
    )


def _equity_dae(*, signed_notional_usd: float = 5_000.0) -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id="P-EQ",
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
# options_delta_pct
# ---------------------------------------------------------------------------


def test_options_delta_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(options_delta_pct=4.2)
    spec = _spec_by_id(build_active_specs(config), "options_delta_pct")
    assert spec.read_current(state, config) == 4.2


def test_options_delta_contribute_signed_for_options() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "options_delta_pct")
    assert spec.contribute(
        _option_proposal(), _option_dae(signed_notional_usd=4_500.0), state, config
    ) == pytest.approx(4.5)


def test_options_delta_contribute_zero_for_equity() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "options_delta_pct")
    assert spec.contribute(_equity_proposal(), _equity_dae(), state, config) == 0.0


# ---------------------------------------------------------------------------
# portfolio_theta_pct_per_day
# ---------------------------------------------------------------------------


def test_portfolio_theta_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(portfolio_theta_pct_per_day=-0.08)
    spec = _spec_by_id(build_active_specs(config), "portfolio_theta_pct_per_day")
    assert spec.read_current(state, config) == -0.08


def test_portfolio_theta_contribute_open_signed() -> None:
    """Theta dollars = net_greeks.theta * quantity * 100; convert to %."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "portfolio_theta_pct_per_day")
    proposal = _option_proposal(quantity=10.0)
    dae = _option_dae(theta=-0.10)
    # theta_dollars = -0.10 * 10 * 100 = -100
    # contribution_pct = -100 / 100_000 * 100 = -0.10
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-0.10)


def test_portfolio_theta_contribute_zero_for_equity() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "portfolio_theta_pct_per_day")
    assert spec.contribute(_equity_proposal(), _equity_dae(), state, config) == 0.0


def test_portfolio_theta_spec_uses_magnitude_flag() -> None:
    config = _config()
    spec = _spec_by_id(build_active_specs(config), "portfolio_theta_pct_per_day")
    assert spec.magnitude is True


def test_portfolio_theta_contribute_close_uses_existing_greeks() -> None:
    """CLOSE on options reads the existing position's current greeks scaled
    by the existing position's contract count.

    The proposal carries ``quantity=3`` (size of the close); the existing
    position has ``quantity=10``. The contribution must use the existing
    position's quantity so the closed theta dollars match what the position
    actually contributed to the book.
    """
    config = _config()
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-OPT": _existing_option_position(quantity=10.0, theta=-0.10)},
    )
    spec = _spec_by_id(build_active_specs(config), "portfolio_theta_pct_per_day")
    proposal = _option_proposal(quantity=3.0, action=Action.CLOSE, existing_position_id="POS-OPT")
    dae = _option_dae(theta=-0.10)
    # CLOSE: after_theta=0; before_theta = -0.10 * 10 (existing.quantity) * 100 = -100
    # contribution = (0 - (-100)) / 100_000 * 100 = +0.10
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(0.10)


def test_portfolio_theta_contribute_add_on_same_strike_is_new_contracts_only() -> None:
    """ADD on an existing options position contributes the new contracts'
    greeks only — the existing position's greeks must not subtract.

    For an ADD where new and existing per-contract theta are equal, the
    naive ``proposal_after - existing_per_contract * proposal_qty`` would
    cancel to zero. The correct contribution scales the new per-contract
    theta by the proposal quantity (the size of the addition).
    """
    config = _config()
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-OPT": _existing_option_position(quantity=10.0, theta=-0.10)},
    )
    spec = _spec_by_id(build_active_specs(config), "portfolio_theta_pct_per_day")
    proposal = _option_proposal(quantity=5.0, action=Action.ADD, existing_position_id="POS-OPT")
    dae = _option_dae(theta=-0.10)
    # theta_dollars = new_per_contract_theta * proposal.quantity * 100 = -0.10 * 5 * 100 = -50
    # contribution_pct = -50 / 100_000 * 100 = -0.05
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-0.05)


def test_portfolio_vega_contribute_add_on_same_strike_is_new_contracts_only() -> None:
    """ADD on options: vega contribution is the new contracts' vega only."""
    config = _config()
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-OPT": _existing_option_position(quantity=10.0, vega=0.20)},
    )
    spec = _spec_by_id(build_active_specs(config), "portfolio_vega_pct_per_iv_point")
    proposal = _option_proposal(quantity=5.0, action=Action.ADD, existing_position_id="POS-OPT")
    dae = _option_dae(vega=0.20)
    # vega_dollars_per_iv_point = 0.20 * 5 * 100 / 100 = 1.0
    # contribution_pct = 1.0 / 100_000 * 100 = 0.001
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(0.001)


def test_portfolio_vega_contribute_close_uses_existing_position_quantity() -> None:
    """CLOSE on options: vega contribution unwinds the existing position's
    vega using the existing position's quantity (not the proposal's)."""
    config = _config()
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-OPT": _existing_option_position(quantity=10.0, vega=0.20)},
    )
    spec = _spec_by_id(build_active_specs(config), "portfolio_vega_pct_per_iv_point")
    proposal = _option_proposal(quantity=3.0, action=Action.CLOSE, existing_position_id="POS-OPT")
    dae = _option_dae(vega=0.20)
    # vega_dollars_per_iv_point = -(existing.vega * existing.quantity * 100 / 100) = -2.0
    # contribution_pct = -2.0 / 100_000 * 100 = -0.002
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-0.002)


# ---------------------------------------------------------------------------
# portfolio_vega_pct_per_iv_point
# ---------------------------------------------------------------------------


def test_portfolio_vega_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(portfolio_vega_pct_per_iv_point=0.15)
    spec = _spec_by_id(build_active_specs(config), "portfolio_vega_pct_per_iv_point")
    assert spec.read_current(state, config) == 0.15


def test_portfolio_vega_contribute_open() -> None:
    """Vega dollars = net_greeks.vega * quantity * 100 / 100 (per 1-IV-point)."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "portfolio_vega_pct_per_iv_point")
    proposal = _option_proposal(quantity=10.0)
    dae = _option_dae(vega=0.20)
    # vega_dollars_per_iv_point = 0.20 * 10 * 100 / 100 = 2.0
    # contribution_pct = 2.0 / 100_000 * 100 = 0.002
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(0.002)


def test_portfolio_vega_spec_uses_magnitude_flag() -> None:
    config = _config()
    spec = _spec_by_id(build_active_specs(config), "portfolio_vega_pct_per_iv_point")
    assert spec.magnitude is True


# ---------------------------------------------------------------------------
# Feature-gated registry filtering
# ---------------------------------------------------------------------------


def test_options_specs_dropped_when_options_disabled() -> None:
    """``feature_flags.options_enabled=False`` removes options rules."""
    config = _config(options_enabled=False)
    rule_ids = {spec.rule_id for spec in build_active_specs(config)}
    assert "options_delta_pct" not in rule_ids
    assert "portfolio_theta_pct_per_day" not in rule_ids
    assert "portfolio_vega_pct_per_iv_point" not in rule_ids


def test_options_specs_present_when_options_enabled() -> None:
    config = _config(options_enabled=True)
    rule_ids = {spec.rule_id for spec in build_active_specs(config)}
    assert "options_delta_pct" in rule_ids
    assert "portfolio_theta_pct_per_day" in rule_ids
    assert "portfolio_vega_pct_per_iv_point" in rule_ids
