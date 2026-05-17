"""Tests for ``build_risk_budget_consumption`` (ALP-503).

Exercises the projection of the in-scope rule registry into a
``RiskBudgetConsumption`` aggregate against a synthetic library snapshot
and config.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from alphamind._kernel.regime import RiskZone
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetEntry
from alphamind.risk_guardrails.guardrail_evaluation import (
    EscalationZones,
    ExistingPosition,
    FeatureFlagsView,
    LibraryConfig,
    PortfolioStateSnapshot,
    build_risk_budget_consumption,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_DEFAULT_ZONES = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)

_DEFAULT_LIMITS: dict[str, float] = {
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


def _config(
    *,
    active_sectors: tuple[str, ...] = ("tech", "semis", "financials", "energy"),
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
    escalation_zones: Mapping[str, EscalationZones] | None = None,
    effective_limits: Mapping[str, float] | None = None,
) -> LibraryConfig:
    limits = dict(_DEFAULT_LIMITS if effective_limits is None else effective_limits)
    zones = (
        {k: _DEFAULT_ZONES for k in limits} if escalation_zones is None else dict(escalation_zones)
    )
    return LibraryConfig(
        effective_limits=MappingProxyType(limits),
        escalation_zones=MappingProxyType(zones),
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
    sector_exposure_pct: Mapping[str, float] | None = None,
    net_long_pct: float = 30.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 30.0,
    position_max_size_pct: float = 5.0,
    total_short_pct: float = 0.0,
    single_short_max_pct: float = 0.0,
    daily_borrow_cost_pct: float = 0.0,
    options_delta_pct: float = 0.0,
    portfolio_theta_pct_per_day: float = 0.0,
    portfolio_vega_pct_per_iv_point: float = 0.0,
    cash_usd: float = 70_000.0,
    portfolio_value_usd: float = 100_000.0,
    reserved_for_pending_orders_usd: float = 0.0,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {
            "tech": 18.0,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        }
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
        existing_positions=MappingProxyType(dict(existing_positions or {})),
    )


def _entries_by_id(entries: tuple[RiskBudgetEntry, ...]) -> dict[str, RiskBudgetEntry]:
    return {e.rule_id: e for e in entries}


# ---------------------------------------------------------------------------
# Rule-coverage tests
# ---------------------------------------------------------------------------


def test_emits_one_sector_entry_per_active_sector() -> None:
    config = _config(active_sectors=("tech", "energy"))
    snapshot = _snapshot(sector_exposure_pct={"tech": 18.0, "energy": 5.0})

    budget = build_risk_budget_consumption(snapshot, config)

    by_id = _entries_by_id(budget.entries)
    assert "sector_concentration_tech" in by_id
    assert "sector_concentration_energy" in by_id
    assert by_id["sector_concentration_tech"].current_value == pytest.approx(18.0)
    assert by_id["sector_concentration_energy"].current_value == pytest.approx(5.0)
    assert by_id["sector_concentration_tech"].limit_value == pytest.approx(25.0)


def test_omits_options_rules_when_options_disabled() -> None:
    config = _config(options_enabled=False)
    snapshot = _snapshot()

    budget = build_risk_budget_consumption(snapshot, config)

    by_id = _entries_by_id(budget.entries)
    assert "options_delta_pct" not in by_id
    assert "portfolio_theta_pct_per_day" not in by_id
    assert "portfolio_vega_pct_per_iv_point" not in by_id


def test_omits_short_rules_when_short_selling_disabled() -> None:
    config = _config(short_selling_enabled=False)
    snapshot = _snapshot()

    budget = build_risk_budget_consumption(snapshot, config)

    by_id = _entries_by_id(budget.entries)
    assert "total_short_pct" not in by_id
    assert "single_short_max_pct" not in by_id
    assert "borrow_cost_budget_pct_per_day" not in by_id
    assert "net_short_pct" not in by_id


def test_emits_full_in_scope_rule_set_with_all_flags_enabled() -> None:
    config = _config()
    snapshot = _snapshot()

    budget = build_risk_budget_consumption(snapshot, config)

    rule_ids = {e.rule_id for e in budget.entries}
    expected = {
        "position_max_size_pct",
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
        "sector_concentration_tech",
        "sector_concentration_semis",
        "sector_concentration_financials",
        "sector_concentration_energy",
    }
    assert rule_ids == expected


# ---------------------------------------------------------------------------
# Per-entry value tests
# ---------------------------------------------------------------------------


def test_headroom_equals_limit_minus_current() -> None:
    config = _config()
    snapshot = _snapshot(net_long_pct=42.5)

    budget = build_risk_budget_consumption(snapshot, config)

    net_long = budget.entry_by_rule_id("net_long_pct")
    assert net_long is not None
    assert net_long.current_value == pytest.approx(42.5)
    assert net_long.limit_value == pytest.approx(60.0)
    assert net_long.headroom == pytest.approx(17.5)


def test_unit_propagates_from_spec() -> None:
    config = _config()
    snapshot = _snapshot()

    budget = build_risk_budget_consumption(snapshot, config)

    net_long = budget.entry_by_rule_id("net_long_pct")
    sector = budget.entry_by_rule_id("sector_concentration_tech")
    assert net_long is not None
    assert sector is not None
    assert net_long.unit == "% of portfolio (delta-adjusted)"
    assert sector.unit == "% of portfolio (delta-adjusted)"


# ---------------------------------------------------------------------------
# Zone classification tests
# ---------------------------------------------------------------------------


def test_zone_normal_when_consumption_below_warning() -> None:
    # 30/60 = 50% of limit; warning starts at 70%
    config = _config()
    snapshot = _snapshot(net_long_pct=30.0)

    budget = build_risk_budget_consumption(snapshot, config)

    net_long = budget.entry_by_rule_id("net_long_pct")
    assert net_long is not None
    assert net_long.zone == RiskZone.NORMAL


def test_zone_warning_at_threshold() -> None:
    # 42/60 = 70%; warning threshold (inclusive)
    config = _config()
    snapshot = _snapshot(net_long_pct=42.0)

    budget = build_risk_budget_consumption(snapshot, config)

    net_long = budget.entry_by_rule_id("net_long_pct")
    assert net_long is not None
    assert net_long.zone == RiskZone.WARNING


def test_zone_critical_at_threshold() -> None:
    # 51/60 = 85%; critical threshold (inclusive)
    config = _config()
    snapshot = _snapshot(net_long_pct=51.0)

    budget = build_risk_budget_consumption(snapshot, config)

    net_long = budget.entry_by_rule_id("net_long_pct")
    assert net_long is not None
    assert net_long.zone == RiskZone.CRITICAL


def test_zone_blocked_at_threshold() -> None:
    # 57/60 = 95%; hard_block threshold (inclusive)
    config = _config()
    snapshot = _snapshot(net_long_pct=57.0)

    budget = build_risk_budget_consumption(snapshot, config)

    net_long = budget.entry_by_rule_id("net_long_pct")
    assert net_long is not None
    assert net_long.zone == RiskZone.BLOCKED


def test_zone_defaults_to_normal_when_zones_missing() -> None:
    # The orchestrator path currently passes escalation_zones={} into LibraryConfig.
    # The builder must defend against this without crashing — emit entries with
    # zone=NORMAL so downstream renderers see a consistent shape.
    config = _config(escalation_zones={})
    # Push net_long_pct well into a would-be-BLOCKED range had zones been
    # present, to prove the NORMAL fallback isn't accidentally aligning with
    # a low-consumption regime.
    snapshot = _snapshot(net_long_pct=58.0)

    budget = build_risk_budget_consumption(snapshot, config)

    net_long = budget.entry_by_rule_id("net_long_pct")
    assert net_long is not None
    assert net_long.zone == RiskZone.NORMAL


# ---------------------------------------------------------------------------
# Determinism + structural tests
# ---------------------------------------------------------------------------


def test_deterministic_equal_inputs_produce_equal_outputs() -> None:
    config = _config()
    snapshot = _snapshot()

    assert build_risk_budget_consumption(snapshot, config) == build_risk_budget_consumption(
        snapshot, config
    )


def test_resolve_sector_entries_finds_every_active_sector() -> None:
    # The analyst's render path is what motivated ALP-503; assert the
    # builder's output satisfies ``resolve_sector_entries`` on every active
    # sector so the original crash site stays green.
    from alphamind.risk_guardrails.state_delivery.primitives import resolve_sector_entries

    config = _config(active_sectors=("tech", "semis", "financials", "energy"))
    snapshot = _snapshot()

    budget = build_risk_budget_consumption(snapshot, config)

    entries = resolve_sector_entries(budget, config.active_sectors)
    assert len(entries) == 4
    assert [e.rule_id for e in entries] == [
        "sector_concentration_tech",
        "sector_concentration_semis",
        "sector_concentration_financials",
        "sector_concentration_energy",
    ]
