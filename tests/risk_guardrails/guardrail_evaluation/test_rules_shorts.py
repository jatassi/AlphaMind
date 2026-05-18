"""Tests for short-specific rule specs (story 04).

Three rules in this category — ``total_short_pct``, ``single_short_max_pct``,
``borrow_cost_budget_pct_per_day`` — all gated on
``feature_flags.short_selling_enabled=True``. Long proposals contribute 0.
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


def _config(*, short_selling_enabled: bool = True) -> LibraryConfig:
    keys = (
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
    )
    return LibraryConfig(
        effective_limits=MappingProxyType({k: 1.0 for k in keys}),
        escalation_zones=MappingProxyType({k: _zones() for k in keys}),
        feature_flags=FeatureFlagsView(
            options_enabled=True,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=("tech",),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _snapshot(
    *,
    portfolio_value_usd: float = 100_000.0,
    total_short_pct: float = 0.0,
    single_short_max_pct: float = 0.0,
    single_short_max_position_id: str | None = None,
    daily_borrow_cost_pct: float = 0.0,
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
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=total_short_pct,
        single_short_max_pct=single_short_max_pct,
        daily_borrow_cost_pct=daily_borrow_cost_pct,
        position_max_size_pct=0.0,
        existing_positions=MappingProxyType(dict(existing_positions)),
        single_short_max_position_id=single_short_max_position_id,
    )


def _short_proposal(
    *,
    proposal_id: str = "P-SHORT",
    notional_usd: float = 4_000.0,
    action: Action = Action.OPEN,
    existing_position_id: str | None = None,
    daily_borrow_cost_usd: float | None = None,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=Symbol("ABC"),
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=money(notional_usd),
        quantity=40.0,
        option_legs=None,
        action=action,
        existing_position_id=existing_position_id,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
    )


def _long_proposal() -> ProposedDelta:
    return ProposedDelta(
        id="P-LONG",
        underlying=Symbol("ABC"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=money(4_000.0),
        quantity=40.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
    )


def _short_dae(*, signed_notional_usd: float = -4_000.0) -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id="P-SHORT",
        signed_notional_usd=signed_notional_usd,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


def _long_dae() -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id="P-LONG",
        signed_notional_usd=4_000.0,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


def _existing_short(
    *,
    notional_usd: float = 5_000.0,
    daily_borrow_cost_usd: float | None = None,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=PositionId("POS-SHORT"),
        underlying=Symbol("ABC"),
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=notional_usd,
        delta_adjusted_exposure_usd=notional_usd,
        current_greeks=None,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital_usd=0.0,
    )


def _spec_by_id(specs: tuple[RuleSpec, ...], rule_id: str) -> RuleSpec:
    for spec in specs:
        if spec.rule_id == rule_id:
            return spec
    raise KeyError(rule_id)


# ---------------------------------------------------------------------------
# total_short_pct
# ---------------------------------------------------------------------------


def test_total_short_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(total_short_pct=18.5)
    spec = _spec_by_id(build_active_specs(config), "total_short_pct")
    assert spec.read_current(state, config) == 18.5


def test_total_short_contribute_open_positive() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "total_short_pct")
    proposal = _short_proposal(action=Action.OPEN, notional_usd=4_000.0)
    dae = _short_dae(signed_notional_usd=-4_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(4.0)


def test_total_short_contribute_close_negative() -> None:
    config = _config()
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-SHORT": _existing_short(notional_usd=5_000.0)},
    )
    spec = _spec_by_id(build_active_specs(config), "total_short_pct")
    proposal = _short_proposal(
        action=Action.CLOSE,
        existing_position_id="POS-SHORT",
    )
    dae = _short_dae(signed_notional_usd=5_000.0)  # short close → +signed_notional
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-5.0)


def test_total_short_contribute_zero_for_long_proposal() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "total_short_pct")
    assert spec.contribute(_long_proposal(), _long_dae(), state, config) == 0.0


# ---------------------------------------------------------------------------
# single_short_max_pct
# ---------------------------------------------------------------------------


def test_single_short_max_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(single_short_max_pct=4.5)
    spec = _spec_by_id(build_active_specs(config), "single_short_max_pct")
    assert spec.read_current(state, config) == 4.5


def test_single_short_max_contribute_zero_when_proposal_smaller_than_current() -> None:
    config = _config()
    state = _snapshot(single_short_max_pct=5.0, portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "single_short_max_pct")
    proposal = _short_proposal(notional_usd=3_000.0)
    dae = _short_dae(signed_notional_usd=-3_000.0)
    assert spec.contribute(proposal, dae, state, config) == 0.0


def test_single_short_max_contribute_delta_when_proposal_larger() -> None:
    config = _config()
    state = _snapshot(single_short_max_pct=3.0, portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "single_short_max_pct")
    proposal = _short_proposal(notional_usd=6_000.0)
    dae = _short_dae(signed_notional_usd=-6_000.0)
    # proposal_pct = 6, current = 3 → contribution 3
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(3.0)


def test_single_short_max_contribute_zero_for_long_proposal() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "single_short_max_pct")
    assert spec.contribute(_long_proposal(), _long_dae(), state, config) == 0.0


def test_single_short_max_read_breaching_position_id_returns_snapshot_field() -> None:
    """AC: the rule projects ``state.single_short_max_position_id`` so the
    continuous-monitor cascade dispatcher routes the close to the same short
    the snapshot identified as the largest.
    """
    config = _config()
    state = _snapshot(
        single_short_max_pct=4.5,
        single_short_max_position_id="POS-SHORT",
    )
    spec = _spec_by_id(build_active_specs(config), "single_short_max_pct")
    assert spec.read_breaching_position_id is not None
    assert spec.read_breaching_position_id(state, config) == "POS-SHORT"


def test_single_short_max_read_breaching_position_id_none_when_no_shorts() -> None:
    config = _config()
    state = _snapshot(single_short_max_pct=0.0, single_short_max_position_id=None)
    spec = _spec_by_id(build_active_specs(config), "single_short_max_pct")
    assert spec.read_breaching_position_id is not None
    assert spec.read_breaching_position_id(state, config) is None


# ---------------------------------------------------------------------------
# borrow_cost_budget_pct_per_day
# ---------------------------------------------------------------------------


def test_borrow_cost_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(daily_borrow_cost_pct=0.012)
    spec = _spec_by_id(build_active_specs(config), "borrow_cost_budget_pct_per_day")
    assert spec.read_current(state, config) == 0.012


def test_borrow_cost_contribute_open_uses_proposal_field() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "borrow_cost_budget_pct_per_day")
    proposal = _short_proposal(action=Action.OPEN, daily_borrow_cost_usd=5.0)
    # 5 / 100_000 * 100 = 0.005
    assert spec.contribute(proposal, _short_dae(), state, config) == pytest.approx(0.005)


def test_borrow_cost_contribute_close_releases_existing_accrual() -> None:
    config = _config()
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={
            "POS-SHORT": _existing_short(daily_borrow_cost_usd=8.0),
        },
    )
    spec = _spec_by_id(build_active_specs(config), "borrow_cost_budget_pct_per_day")
    proposal = _short_proposal(action=Action.CLOSE, existing_position_id="POS-SHORT")
    # -8 / 100_000 * 100 = -0.008
    assert spec.contribute(proposal, _short_dae(), state, config) == pytest.approx(-0.008)


def test_borrow_cost_contribute_zero_for_long() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "borrow_cost_budget_pct_per_day")
    assert spec.contribute(_long_proposal(), _long_dae(), state, config) == 0.0


def test_borrow_cost_contribute_zero_when_proposal_field_missing() -> None:
    """No daily_borrow_cost_usd on proposal → contribute 0 (deferred upstream)."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "borrow_cost_budget_pct_per_day")
    proposal = _short_proposal(action=Action.OPEN, daily_borrow_cost_usd=None)
    assert spec.contribute(proposal, _short_dae(), state, config) == 0.0


# ---------------------------------------------------------------------------
# Feature-gated registry filtering
# ---------------------------------------------------------------------------


def test_short_specs_dropped_when_short_selling_disabled() -> None:
    config = _config(short_selling_enabled=False)
    rule_ids = {spec.rule_id for spec in build_active_specs(config)}
    assert "total_short_pct" not in rule_ids
    assert "single_short_max_pct" not in rule_ids
    assert "borrow_cost_budget_pct_per_day" not in rule_ids


def test_short_specs_present_when_short_selling_enabled() -> None:
    config = _config(short_selling_enabled=True)
    rule_ids = {spec.rule_id for spec in build_active_specs(config)}
    assert "total_short_pct" in rule_ids
    assert "single_short_max_pct" in rule_ids
    assert "borrow_cost_budget_pct_per_day" in rule_ids
