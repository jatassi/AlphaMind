"""Tests for the rule-contribution registry and ``project_all`` (story 04).

Covers sector-concentration spec generation, registry order stability, and
the end-to-end ``project_all`` orchestration over a small synthetic portfolio.
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
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
    Status,
    build_active_specs,
    project_all,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _full_config(
    *,
    active_sectors: tuple[str, ...] = ("tech", "semis", "financials", "energy"),
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
) -> LibraryConfig:
    """All 13 in-scope rules enabled."""
    static_keys = (
        "position_max_size_pct",
        "sector_concentration_pct",
        "net_long_pct",
        "net_short_pct",
        "gross_exposure_pct",
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
        "min_cash_reserve_pct",
        "pending_order_capital_pct",
    )
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
    assert set(effective_limits) == set(static_keys)
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
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
    net_long_pct: float = 30.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 30.0,
    position_max_size_pct: float = 5.0,
    total_short_pct: float = 0.0,
    single_short_max_pct: float = 0.0,
    daily_borrow_cost_pct: float = 0.0,
    options_delta_pct: float = 0.0,
    cash_usd: float = 70_000.0,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {
            "tech": 18.0,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        }
    if existing_positions is None:
        existing_positions = {}
    return PortfolioStateSnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct)),
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        gross_pct=gross_pct,
        options_delta_pct=options_delta_pct,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=total_short_pct,
        single_short_max_pct=single_short_max_pct,
        daily_borrow_cost_pct=daily_borrow_cost_pct,
        position_max_size_pct=position_max_size_pct,
        existing_positions=MappingProxyType(dict(existing_positions)),
    )


def _equity_proposal(
    *,
    proposal_id: str,
    sector: str,
    direction: Direction,
    notional_usd: float,
    daily_borrow_cost_usd: float | None = None,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=Symbol("ABC"),
        sector=sector,
        direction=direction,
        asset_type=AssetType.EQUITY,
        notional_usd=notional_usd,
        quantity=notional_usd / 100.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
    )


def _equity_dae(*, proposal_id: str, signed_notional_usd: float) -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id=proposal_id,
        signed_notional_usd=signed_notional_usd,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


# ---------------------------------------------------------------------------
# Sector-concentration registry expansion
# ---------------------------------------------------------------------------


def test_sector_concentration_spec_generation_per_active_sector() -> None:
    config = _full_config(active_sectors=("tech", "semis", "financials", "energy"))
    rule_ids = {spec.rule_id for spec in build_active_specs(config)}
    expected = {
        "sector_concentration_tech",
        "sector_concentration_semis",
        "sector_concentration_financials",
        "sector_concentration_energy",
    }
    assert expected.issubset(rule_ids)


def test_sector_concentration_uses_shared_effective_limit_key() -> None:
    """Per-sector specs share ``sector_concentration_pct`` as effective_limit_key."""
    config = _full_config(active_sectors=("tech", "semis"))
    sector_specs = [
        spec
        for spec in build_active_specs(config)
        if spec.rule_id.startswith("sector_concentration_")
    ]
    assert len(sector_specs) == 2
    for spec in sector_specs:
        assert spec.effective_limit_key == "sector_concentration_pct"


def test_sector_concentration_specs_dropped_when_limit_absent() -> None:
    """If ``sector_concentration_pct`` isn't in effective_limits, no sector specs."""
    config = _full_config()
    # Drop the sector-concentration limit
    new_limits = {
        k: v for k, v in config.effective_limits.items() if k != "sector_concentration_pct"
    }
    new_zones = {
        k: v for k, v in config.escalation_zones.items() if k != "sector_concentration_pct"
    }
    config2 = LibraryConfig(
        effective_limits=MappingProxyType(new_limits),
        escalation_zones=MappingProxyType(new_zones),
        feature_flags=config.feature_flags,
        active_sectors=config.active_sectors,
        active_regime=config.active_regime,
        active_profile=config.active_profile,
        conservative_buffer_pct=config.conservative_buffer_pct,
    )
    rule_ids = {spec.rule_id for spec in build_active_specs(config2)}
    assert not any(rid.startswith("sector_concentration_") for rid in rule_ids)


# ---------------------------------------------------------------------------
# Stable order
# ---------------------------------------------------------------------------


def test_build_active_specs_returns_stable_order() -> None:
    config = _full_config()
    a = tuple(spec.rule_id for spec in build_active_specs(config))
    b = tuple(spec.rule_id for spec in build_active_specs(config))
    assert a == b


def test_build_active_specs_returns_lexicographic_order() -> None:
    config = _full_config()
    rule_ids = [spec.rule_id for spec in build_active_specs(config)]
    assert rule_ids == sorted(rule_ids)


# ---------------------------------------------------------------------------
# project_all end-to-end
# ---------------------------------------------------------------------------


def test_project_all_two_proposals_documented_per_rule_set() -> None:
    """Construct a small portfolio state with two proposals; assert the
    resulting per_rule includes the documented set with correct status."""
    config = _full_config(
        active_sectors=("tech", "semis"),
        short_selling_enabled=True,
        options_enabled=False,  # options absent → no greek rules
    )
    # Drop option keys from config since options is disabled — but actually
    # they remain in effective_limits; the registry's requires_options filter
    # drops them. Keep config simple.
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        net_long_pct=20.0,
        gross_pct=20.0,
        position_max_size_pct=5.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        sector_exposure_pct={"tech": 10.0, "semis": 0.0},
    )
    long_tech = _equity_proposal(
        proposal_id="P-LONG",
        sector="tech",
        direction=Direction.LONG,
        notional_usd=5_000.0,
    )
    short_semis = _equity_proposal(
        proposal_id="P-SHORT",
        sector="semis",
        direction=Direction.SHORT,
        notional_usd=4_000.0,
        daily_borrow_cost_usd=2.0,
    )
    proposals_with_dae = (
        (long_tech, _equity_dae(proposal_id="P-LONG", signed_notional_usd=5_000.0)),
        (short_semis, _equity_dae(proposal_id="P-SHORT", signed_notional_usd=-4_000.0)),
    )
    projections = project_all(
        proposals_with_dae=proposals_with_dae,
        state=state,
        config=config,
    )
    by_rule = {p.rule: p for p in projections}

    # Check the expected rules are present
    expected_present = {
        "position_max_size_pct",
        "net_long_pct",
        "net_short_pct",
        "gross_exposure_pct",
        "sector_concentration_tech",
        "sector_concentration_semis",
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
        "min_cash_reserve_pct",
        "pending_order_capital_pct",
    }
    assert expected_present.issubset(by_rule.keys())

    # Options rules should be absent under options_enabled=False
    options_rules = {
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
    }
    assert options_rules.isdisjoint(by_rule.keys())

    # Net long: current=20, +5 from long, -4 from short = 21
    net_long = by_rule["net_long_pct"]
    assert net_long.current == pytest.approx(20.0)
    assert net_long.projected_after == pytest.approx(21.0)
    assert net_long.status is Status.PASS

    # Net short: current=0; long OPEN contributes -5, short OPEN contributes +4 → -1
    # (negative projected_after against a positive max-net-short limit classifies PASS)
    net_short = by_rule["net_short_pct"]
    assert net_short.projected_after == pytest.approx(-1.0)
    assert net_short.status is Status.PASS

    # Gross: current=20, +5 (long open) + 4 (short open absolute) = 29
    gross = by_rule["gross_exposure_pct"]
    assert gross.projected_after == pytest.approx(29.0)

    # Sector tech: current 10, +5 from long → 15
    assert by_rule["sector_concentration_tech"].projected_after == pytest.approx(15.0)
    # Sector semis: current 0, -4 from short OPEN (sector matches, signed) → -4
    assert by_rule["sector_concentration_semis"].projected_after == pytest.approx(-4.0)

    # Total short: current 0, +4 from short open
    assert by_rule["total_short_pct"].projected_after == pytest.approx(4.0)

    # Position max: current=5; long=5%, short=4% → no contribution (neither exceeds 5)
    assert by_rule["position_max_size_pct"].projected_after == pytest.approx(5.0)


def test_project_all_under_micro_profile_no_options_or_short_rules() -> None:
    """Closure under feature flags: options & shorts disabled."""
    config = _full_config(
        active_sectors=("tech",),
        options_enabled=False,
        short_selling_enabled=False,
    )
    state = _snapshot()
    projections = project_all(
        proposals_with_dae=(),
        state=state,
        config=config,
    )
    rule_ids = {p.rule for p in projections}
    options_rules = {
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
    }
    short_rules = {
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
    }
    assert options_rules.isdisjoint(rule_ids)
    assert short_rules.isdisjoint(rule_ids)


def test_project_all_returns_per_rule_in_registry_order() -> None:
    """``per_rule`` order matches ``build_active_specs`` order (lexicographic)."""
    config = _full_config(active_sectors=("tech",))
    state = _snapshot()
    projections = project_all(proposals_with_dae=(), state=state, config=config)
    rule_ids = [p.rule for p in projections]
    assert rule_ids == sorted(rule_ids)


def test_project_all_propagates_breaching_position_id_for_single_short_max() -> None:
    """``single_short_max_pct`` projection carries the snapshot's identified short.

    The continuous-monitor cascade dispatcher reads this field off the
    ``RuleEvaluation`` projected from this ``RuleProjection``; without it,
    the dispatcher would have to re-scan ``existing_positions`` (the
    pre-ALP-511 lossy fallback).
    """
    config = _full_config(active_sectors=("tech",))
    state = PortfolioStateSnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": 0.0}),
        net_long_pct=0.0,
        net_short_pct=0.0,
        gross_pct=0.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=8.0,
        single_short_max_pct=6.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
        single_short_max_position_id="POS-BIGGEST-SHORT",
    )
    projections = project_all(proposals_with_dae=(), state=state, config=config)
    single_short = next(p for p in projections if p.rule == "single_short_max_pct")

    assert single_short.breaching_position_id == "POS-BIGGEST-SHORT"


def test_project_all_breaching_position_id_none_for_portfolio_scope_rules() -> None:
    """Rules without a ``read_breaching_position_id`` hook project ``None``."""
    config = _full_config(active_sectors=("tech",))
    state = _snapshot(single_short_max_pct=0.0)
    projections = project_all(proposals_with_dae=(), state=state, config=config)

    portfolio_scope_rules = {
        "net_long_pct",
        "net_short_pct",
        "gross_exposure_pct",
        "total_short_pct",
        "daily_drawdown_pct",
        "cumulative_drawdown_pct",
    }
    for projection in projections:
        if projection.rule in portfolio_scope_rules:
            assert projection.breaching_position_id is None


# ---------------------------------------------------------------------------
# ProposedDelta extension fields (verifies story-01 spec extensions)
# ---------------------------------------------------------------------------


def test_proposed_delta_has_daily_borrow_cost_usd_default_none() -> None:
    proposal = ProposedDelta(
        id="P",
        underlying=Symbol("ABC"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=1_000.0,
        quantity=10.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
    )
    assert proposal.daily_borrow_cost_usd is None


def test_proposed_delta_has_reserves_capital_default_false() -> None:
    proposal = ProposedDelta(
        id="P",
        underlying=Symbol("ABC"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=1_000.0,
        quantity=10.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
    )
    assert proposal.reserves_capital is False
