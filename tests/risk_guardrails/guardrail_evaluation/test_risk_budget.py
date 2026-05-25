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


def test_raises_when_zones_missing_for_a_rule() -> None:
    # Every caller path builds the config through ``from_resolved_config``,
    # which fills zones for every rule. A missing entry is a real bug — silent
    # NORMAL classifications can't hide a consumption breach.
    config = _config(escalation_zones={})
    snapshot = _snapshot()

    with pytest.raises(KeyError):
        build_risk_budget_consumption(snapshot, config)


# ---------------------------------------------------------------------------
# Inverse-rule classification tests
# ---------------------------------------------------------------------------


def test_min_cash_reserve_healthy_classifies_normal_and_not_breaching() -> None:
    # ``min_cash_reserve_pct`` is the only inverse rule in the registry — the
    # limit is a floor, so healthy = current >= limit. A naive cap-rule
    # classifier (consumption_pct = current / limit * 100, BLOCKED at >= 95)
    # would have flagged this as BLOCKED and leaked a phantom hard-block line
    # into every agent prompt.
    config = _config()
    # cash_usd=25_700 against portfolio_value=100_000 gives cash_pct=25.7%,
    # well above the 10% floor.
    snapshot = _snapshot(cash_usd=25_700.0)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("min_cash_reserve_pct")
    assert entry is not None
    assert entry.current_value == pytest.approx(25.7)
    assert entry.limit_value == pytest.approx(10.0)
    assert entry.zone == RiskZone.NORMAL
    assert entry not in budget.breaching_entries()


def test_min_cash_reserve_within_warning_band_classifies_warning() -> None:
    # inverse_warning_band_pct=20 → warning floor = 10.0 * 1.20 = 12.0.
    # current=11.0% lands in [10, 12) → WARNING.
    config = _config()
    snapshot = _snapshot(cash_usd=11_000.0)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("min_cash_reserve_pct")
    assert entry is not None
    assert entry.zone == RiskZone.WARNING


def test_min_cash_reserve_below_floor_classifies_blocked() -> None:
    # current=5% < limit=10% → BLOCKED (FAIL in projection-engine terms).
    config = _config()
    snapshot = _snapshot(cash_usd=5_000.0)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("min_cash_reserve_pct")
    assert entry is not None
    assert entry.zone == RiskZone.BLOCKED
    assert entry in budget.breaching_entries()


# ---------------------------------------------------------------------------
# headroom_pct_of_limit tests
# ---------------------------------------------------------------------------


def test_headroom_pct_cap_rule_normal_consumption() -> None:
    # net_long_pct=30 against limit=60 → headroom=30 → headroom_pct=50%.
    config = _config()
    snapshot = _snapshot(net_long_pct=30.0)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("net_long_pct")
    assert entry is not None
    assert entry.headroom_pct_of_limit == pytest.approx(50.0)


def test_headroom_pct_cap_rule_above_limit_clamps_to_zero() -> None:
    # net_long_pct=70 > limit=60 → headroom=-10 → clamped headroom_pct=0.
    config = _config()
    snapshot = _snapshot(net_long_pct=70.0)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("net_long_pct")
    assert entry is not None
    assert entry.headroom_pct_of_limit == 0.0


def test_headroom_pct_inverse_rule_above_floor() -> None:
    # cash=15%, limit=10% → buffer = 5%, buffer/limit = 50%.
    config = _config()
    snapshot = _snapshot(cash_usd=15_000.0)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("min_cash_reserve_pct")
    assert entry is not None
    assert entry.headroom_pct_of_limit == pytest.approx(50.0)


def test_headroom_pct_inverse_rule_below_floor_clamps_to_zero() -> None:
    # cash=5%, limit=10% → buffer negative → clamped to 0.
    config = _config()
    snapshot = _snapshot(cash_usd=5_000.0)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("min_cash_reserve_pct")
    assert entry is not None
    assert entry.headroom_pct_of_limit == 0.0


# ---------------------------------------------------------------------------
# Magnitude-rule classification tests
# ---------------------------------------------------------------------------


def test_magnitude_rule_accepts_negative_current_and_classifies_on_absolute_value() -> None:
    # Long-options portfolios carry negative theta by construction. The bug
    # this regression covers: ``_classify_zone`` used to reject any
    # ``current_value < 0`` outright, crashing the decision pipeline before a
    # single agent ran. Magnitude rules (``magnitude=True``) must classify on
    # ``|current|`` instead — mirroring projection.py:143.
    config = _config()
    snapshot = _snapshot(portfolio_theta_pct_per_day=-0.021)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("portfolio_theta_pct_per_day")
    assert entry is not None
    # |-0.021| / 0.5 = 4.2% consumption → NORMAL (warning starts at 70%).
    assert entry.current_value == pytest.approx(-0.021)
    assert entry.zone == RiskZone.NORMAL


def test_magnitude_rule_classifies_warning_on_negative_magnitude() -> None:
    # |-0.4| / 0.5 = 80% consumption → WARNING band ([70, 85)).
    config = _config()
    snapshot = _snapshot(portfolio_theta_pct_per_day=-0.4)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("portfolio_theta_pct_per_day")
    assert entry is not None
    assert entry.zone == RiskZone.WARNING


def test_magnitude_rule_headroom_pct_uses_absolute_value() -> None:
    # vega limit is 1.0; |-0.5| / 1.0 = 50% consumption → 50% headroom.
    # A naive signed read would have computed (1.0 - (-0.5))/1.0 = 150%
    # (clamped to 100), masking the real consumption.
    config = _config()
    snapshot = _snapshot(portfolio_vega_pct_per_iv_point=-0.5)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("portfolio_vega_pct_per_iv_point")
    assert entry is not None
    assert entry.headroom_pct_of_limit == pytest.approx(50.0)


def test_magnitude_rule_headroom_field_stays_signed() -> None:
    # RiskBudgetEntry validates ``headroom == limit_value - current_value``
    # with strict equality. The magnitude flag affects classification +
    # headroom_pct, NOT the signed headroom field.
    config = _config()
    snapshot = _snapshot(portfolio_theta_pct_per_day=-0.1)

    budget = build_risk_budget_consumption(snapshot, config)

    entry = budget.entry_by_rule_id("portfolio_theta_pct_per_day")
    assert entry is not None
    assert entry.headroom == pytest.approx(0.5 - (-0.1))


# ---------------------------------------------------------------------------
# Rule-label tests
# ---------------------------------------------------------------------------


def test_rule_label_uses_human_readable_form() -> None:
    config = _config()
    snapshot = _snapshot()

    budget = build_risk_budget_consumption(snapshot, config)

    by_id = _entries_by_id(budget.entries)
    assert by_id["net_long_pct"].rule_label == "Net long"
    assert by_id["min_cash_reserve_pct"].rule_label == "Min cash reserve"
    assert by_id["sector_concentration_tech"].rule_label == "Tech concentration"


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
