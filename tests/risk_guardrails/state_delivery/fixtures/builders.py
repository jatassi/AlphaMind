"""Shared fixture builders for state-delivery renderer tests (ALP-791).

_make_budget_entry is repeated verbatim in 6 test modules.
_make_param_entry, the canonical (no-arg) _make_state_delivery_config,
_DEFAULT_POSITION_ZONES, _make_directional, and _make_thesis_quality_aggregates
appear in >=2 files each.

These are pure test infrastructure. No production code imports this module.
Moving the bodies here + deleting local copies in the test files eliminates
~duplication without altering test behavior, collected count, or assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.money import money
from alphamind._kernel.regime import RiskZone
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetEntry
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
)
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.snapshot import DirectionalExposure
from alphamind.risk_guardrails.guardrail_evaluation.types import EscalationZones
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

_DEFAULT_POSITION_ZONES = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


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


def _make_state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
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
    )
