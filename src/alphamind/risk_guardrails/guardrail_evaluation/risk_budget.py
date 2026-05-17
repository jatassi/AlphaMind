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

from alphamind._kernel.regime import RiskZone, classify_consumption_zone
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.risk_guardrails.guardrail_evaluation.projection import (
    MIN_RULE_WARNING_BAND_PCT,
)
from alphamind.risk_guardrails.guardrail_evaluation.rules import build_active_specs
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    EscalationZones,
    LibraryConfig,
    PortfolioStateSnapshot,
)

__all__ = ["build_risk_budget_consumption"]


# Display labels for the canonical rule_ids. Sector-concentration rule_ids
# (``sector_concentration_<sector>``) are not enumerated here; the renderer's
# sector-label resolver handles those.
_RULE_LABELS: dict[str, str] = {
    "position_max_size_pct": "Position max size",
    "net_long_pct": "Net long",
    "net_short_pct": "Net short",
    "gross_exposure_pct": "Gross exposure",
    "options_delta_pct": "Options delta",
    "portfolio_theta_pct_per_day": "Portfolio theta",
    "portfolio_vega_pct_per_iv_point": "Portfolio vega",
    "total_short_pct": "Total short",
    "single_short_max_pct": "Single short max",
    "borrow_cost_budget_pct_per_day": "Borrow cost budget",
    "min_cash_reserve_pct": "Min cash reserve",
    "pending_order_capital_pct": "Pending order capital",
}


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
    * ``zone`` classifies via the per-rule escalation zones. Inverse rules
      (``spec.inverse=True``) mirror the projection engine's convention
      (``projection.py:_classify``) — FAIL below the floor, WARNING within
      ``MIN_RULE_WARNING_BAND_PCT`` above the floor, NORMAL otherwise —
      collapsed to three of the four ``RiskZone`` values
      (NORMAL / WARNING / BLOCKED; CRITICAL is unused for inverse). A rule
      missing from ``config.escalation_zones`` raises ``KeyError``; both
      caller paths (scheduler and breach loop) build the config through
      ``from_resolved_config``, which fills the zones for every rule.

    ``cumulative_invocation_impact_value`` is ``0.0`` at snapshot-build
    time: no proposals have been validated yet. ``headroom_pct_of_limit``
    is the fraction of headroom remaining relative to the limit, clamped
    to ``[0, 100]`` — for inverse rules it measures buffer above the floor
    instead of below the cap.
    """
    entries: list[RiskBudgetEntry] = []
    for spec in build_active_specs(config):
        current = spec.read_current(snapshot, config)
        limit = config.effective_limits[spec.effective_limit_key]
        zones = config.escalation_zones[spec.effective_limit_key]
        entries.append(
            RiskBudgetEntry(
                rule_id=spec.rule_id,
                rule_label=_rule_label(spec.rule_id),
                current_value=current,
                limit_value=limit,
                headroom=limit - current,
                headroom_pct_of_limit=_headroom_pct(current, limit, inverse=spec.inverse),
                zone=_classify_zone(current, limit, zones, inverse=spec.inverse),
                unit=spec.unit,
                cumulative_invocation_impact_value=0.0,
            )
        )
    return RiskBudgetConsumption(entries=tuple(entries))


def _rule_label(rule_id: str) -> str:
    """Return a human-readable label for *rule_id*.

    Sector-concentration ids (``sector_concentration_<sector>``) fall through
    to a ``"<Sector> concentration"`` form; the renderer's sector-label
    resolver overlays a richer display name when one is configured.
    """
    if rule_id in _RULE_LABELS:
        return _RULE_LABELS[rule_id]
    if rule_id.startswith("sector_concentration_"):
        sector = rule_id.removeprefix("sector_concentration_")
        return f"{sector.capitalize()} concentration"
    return rule_id


def _headroom_pct(current_value: float, limit_value: float, *, inverse: bool) -> float:
    """Compute ``headroom_pct_of_limit`` clamped to ``[0, 100]``.

    For cap rules: how much of the limit remains unconsumed. For inverse
    (floor) rules: how far above the floor the current value sits, as a
    fraction of the floor.
    """
    if limit_value <= 0:
        return 0.0
    raw = (current_value - limit_value) if inverse else (limit_value - current_value)
    return max(0.0, min(100.0, raw / limit_value * 100.0))


def _classify_zone(
    current_value: float,
    limit_value: float,
    zones: EscalationZones,
    *,
    inverse: bool,
) -> RiskZone:
    """Classify the rule's risk zone from current vs. limit.

    Cap rules (``inverse=False``) delegate to ``classify_consumption_zone``
    against the supplied escalation thresholds. Inverse rules mirror the
    projection engine's convention (``projection.py:_classify``): FAIL
    (→ BLOCKED) below the floor, WARNING within ``MIN_RULE_WARNING_BAND_PCT``
    above the floor, NORMAL otherwise.

    Raises ``ValueError`` on ``limit_value <= 0`` or ``current_value < 0``
    (real data corruption that the rest of the system would refuse).
    """
    if limit_value <= 0:
        msg = f"limit_value must be > 0; got {limit_value}"
        raise ValueError(msg)
    if current_value < 0:
        msg = f"current_value must be >= 0; got {current_value}"
        raise ValueError(msg)
    if inverse:
        warning_floor = limit_value * (1 + MIN_RULE_WARNING_BAND_PCT / 100.0)
        if current_value >= warning_floor:
            return RiskZone.NORMAL
        if current_value >= limit_value:
            return RiskZone.WARNING
        return RiskZone.BLOCKED
    return classify_consumption_zone(
        consumption_pct=current_value / limit_value * 100.0,
        warning=zones.warning,
        critical=zones.critical,
        hard_block=zones.hard_block,
    )
