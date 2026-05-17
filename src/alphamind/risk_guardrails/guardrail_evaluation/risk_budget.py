"""Project the current portfolio's risk-budget consumption (ALP-503).

Pure function — iterates ``build_active_specs(config)`` to produce one
``RiskBudgetEntry`` per in-scope rule, reusing the rule registry's
``read_current`` so the per-rule reader logic stays in one place. Output
slots into the snapshot's ``risk_budget`` field for downstream renderers
(analyst / strategist / portfolio_manager input bundles).

Replaces the pre-ALP-503 empty stub the SQL repository returned for
``get_risk_budget_consumption`` — the decision pipeline now overrides the
snapshot's ``risk_budget`` with the projected consumption between the
library-shape translation and the per-consumer view projection.
"""

from __future__ import annotations

from alphamind._kernel.regime import RiskZone
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.risk_guardrails.guardrail_evaluation.rules import build_active_specs
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    EscalationZones,
    LibraryConfig,
    PortfolioStateSnapshot,
)

__all__ = ["build_risk_budget_consumption"]


def build_risk_budget_consumption(
    snapshot: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> RiskBudgetConsumption:
    """Project the in-scope risk-budget entries for *snapshot* under *config*.

    For each spec returned by ``build_active_specs(config)``:

    * ``current_value = spec.read_current(snapshot, config)``
    * ``limit_value = config.effective_limits[spec.effective_limit_key]``
    * ``headroom = limit_value - current_value`` (signed; matches the
      ``RiskBudgetEntry`` validator's strict equality contract).
    * ``zone`` classifies ``current / limit`` against the per-rule
      escalation zones when present. Rules missing from
      ``config.escalation_zones`` default to ``RiskZone.NORMAL`` — the
      scheduler-orchestrator path currently passes an empty zone mapping,
      and the builder should remain usable across both that path and the
      breach-loop path (which uses ``from_resolved_config`` and has
      populated zones).

    ``headroom_pct_of_limit`` and ``cumulative_invocation_impact_value``
    are zero: the former is in the typed contract but no renderer
    consumes it; the latter is populated by within-invocation
    accumulators that don't exist at snapshot-build time.
    """
    entries: list[RiskBudgetEntry] = []
    for spec in build_active_specs(config):
        if _excluded_by_feature_flag(spec.rule_id, config):
            continue
        current = spec.read_current(snapshot, config)
        limit = config.effective_limits[spec.effective_limit_key]
        zones = config.escalation_zones.get(spec.effective_limit_key)
        entries.append(
            RiskBudgetEntry(
                rule_id=spec.rule_id,
                rule_label=spec.rule_id,
                current_value=current,
                limit_value=limit,
                headroom=limit - current,
                headroom_pct_of_limit=0.0,
                zone=_classify_zone(current, limit, zones),
                unit=spec.unit,
                cumulative_invocation_impact_value=0.0,
            )
        )
    return RiskBudgetConsumption(entries=tuple(entries))


def _excluded_by_feature_flag(rule_id: str, config: LibraryConfig) -> bool:
    """Mirror ``validate_feature_flag_closure``'s rejection set.

    ``build_active_specs`` filters by each spec's ``requires_shorts`` /
    ``requires_options`` flag. ``net_short_pct`` carries neither flag in
    the rule library (it's classified as an exposure rule), but the
    analyst's renderer-side validator
    (``state_delivery.primitives.validate_feature_flag_closure``) refuses
    to render a ``net_short_pct`` entry when short selling is disabled.
    Drop it here so the budget matches the renderer's contract.
    """
    return rule_id == "net_short_pct" and not config.feature_flags.short_selling_enabled


def _classify_zone(
    current_value: float,
    limit_value: float,
    zones: EscalationZones | None,
) -> RiskZone:
    """Mirror ``breach_behavior.zones.classify_zone`` for the library-shape
    :class:`EscalationZones`.

    Defends against missing zones, non-positive limits, and negative current
    values — all shape-level cases the strict ``classify_zone`` raises on but
    the builder must keep tolerating because the upstream config builder
    surface accepts them (e.g., the scheduler orchestrator's
    ``escalation_zones={}``).
    """
    if zones is None or limit_value <= 0 or current_value < 0:
        return RiskZone.NORMAL
    consumption_pct = current_value / limit_value * 100.0
    if consumption_pct >= zones.hard_block:
        return RiskZone.BLOCKED
    if consumption_pct >= zones.critical:
        return RiskZone.CRITICAL
    if consumption_pct >= zones.warning:
        return RiskZone.WARNING
    return RiskZone.NORMAL
