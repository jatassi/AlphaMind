"""Regime-transition breach detector (story 07).

Pure function that, after the new effective limits are resolved for the current
invocation, scans the held positions and the risk-budget snapshot and emits one
``RegimeTransitionBreach`` per breaching (rule, position) pair. Surfaces the
deferred-to-strategist breaches the design specifies — the strategist's
guardrail state header renders a ``Regime-transition breaches`` block from this
output, and the strategist proposes thesis-informed remedies the PM reviews.

Reading: the story file at
``docs/implementation/06-risk-guardrails/regime-adaptation/07-regime-transition-breach-detector.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

from alphamind._kernel.regime import RegimeTransitionState
from alphamind.portfolio_state.records.positions import Direction, position_direction
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.regime_adaptation.types import (
    RegimeTransitionBreach,
    RuleMetadata,
)

if TYPE_CHECKING:
    # ``RiskBudgetConsumption`` lives in ``portfolio_state.records.capital``,
    # which re-exports the four risk-guardrail enums from
    # ``risk_guardrails.regime_adaptation.types`` (and siblings). Importing it
    # at runtime would cycle through capital → regime_adaptation package init
    # → this module. The annotation-only usage is safe under
    # ``from __future__ import annotations``.
    from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption

# ---------------------------------------------------------------------------
# Deferred-classification rule set (per breach-behavior.md § Per-rule breach
# response classification, restricted to the regime-transition deferred
# surface). Closed enumeration; not a tunable.
# ---------------------------------------------------------------------------

_DEFERRED_RULE_IDS: frozenset[str] = frozenset(
    {
        "position_max_size_pct",
        "single_short_max_pct",
        "sector_concentration_pct",
        "net_long_pct",
        "net_short_pct",
        "gross_exposure_pct",
        "options_delta_pct",
    }
)
"""The closed set of rules whose breach response is *deferred to strategist →
PM* and whose breaches the regime-transition detector emits. The
``sector_concentration_pct`` entry is the rule-id stem; the resolver fans it
out per active sector at runtime (e.g., ``sector_concentration_tech``)."""


_RULE_EMISSION_KIND: Mapping[str, Literal["per_position", "aggregate"]] = {
    "position_max_size_pct": "per_position",
    "single_short_max_pct": "per_position",
    "sector_concentration_pct": "aggregate",
    "net_long_pct": "aggregate",
    "net_short_pct": "aggregate",
    "gross_exposure_pct": "aggregate",
    "options_delta_pct": "aggregate",
}
"""Per-rule emission classification — *per-position* rules emit one record per
breaching position with ``position_id`` set; *aggregate* rules emit one record
per breaching rule with ``position_id=None``."""


def detect_regime_transition_breaches(
    *,
    held_positions: tuple[PositionView, ...],
    risk_budget: RiskBudgetConsumption,
    new_effective_limits: Mapping[str, float],
    transition_state: RegimeTransitionState,
    rule_metadata: Mapping[str, RuleMetadata],
) -> tuple[RegimeTransitionBreach, ...]:
    """Return the regime-transition breaches for the current invocation.

    Pure function; equal inputs produce equal outputs and no input collection
    is mutated. Returns ``()`` when ``transition_state`` is not ``TIGHTENING``
    or when there are no positions: regime-transition breaches arise only from
    tightening transitions (loosening relaxes limits gradually; stable means
    no change).

    Output is sorted ascending by ``(rule_id, position_id_or_empty_string)``;
    aggregate records (``position_id=None``) sort before per-position records
    sharing the same ``rule_id``.
    """
    if transition_state != RegimeTransitionState.TIGHTENING:
        return ()
    if not held_positions:
        return ()

    breaches: list[RegimeTransitionBreach] = []
    breaches.extend(
        _scan_per_position_rule(
            rule_id="position_max_size_pct",
            held_positions=held_positions,
            new_effective_limits=new_effective_limits,
            rule_metadata=rule_metadata,
            position_filter=lambda _position: True,
        )
    )
    breaches.extend(
        _scan_per_position_rule(
            rule_id="single_short_max_pct",
            held_positions=held_positions,
            new_effective_limits=new_effective_limits,
            rule_metadata=rule_metadata,
            # position_direction() yields None for a strategy, which compares
            # unequal to Direction.SHORT — a strategy is correctly excluded
            # from the short-position set for single_short_max_pct.
            position_filter=lambda position: position_direction(position.record) == Direction.SHORT,
        )
    )
    breaches.extend(
        _scan_aggregate_rules(
            risk_budget=risk_budget,
            new_effective_limits=new_effective_limits,
            rule_metadata=rule_metadata,
        )
    )
    breaches.sort(key=lambda b: (b.rule_id, b.position_id or ""))
    return tuple(breaches)


def _classify_rule_id(rule_id: str) -> Literal["per_position", "aggregate"] | None:
    """Resolve the emission kind for a rule id, handling the sector-prefix
    fan-out: ``sector_concentration_*`` rule ids share the
    ``sector_concentration_pct`` classification.

    Returns ``None`` if the id is not in the deferred set.
    """
    if rule_id in _RULE_EMISSION_KIND:
        return _RULE_EMISSION_KIND[rule_id]
    if rule_id.startswith("sector_concentration_"):
        return _RULE_EMISSION_KIND["sector_concentration_pct"]
    return None


def _scan_per_position_rule(
    *,
    rule_id: str,
    held_positions: tuple[PositionView, ...],
    new_effective_limits: Mapping[str, float],
    rule_metadata: Mapping[str, RuleMetadata],
    position_filter: Callable[[PositionView], bool],
) -> list[RegimeTransitionBreach]:
    new_limit = _require(new_effective_limits, rule_id, source="new_effective_limits")
    metadata = _require(rule_metadata, rule_id, source="rule_metadata")
    out: list[RegimeTransitionBreach] = []
    for position in held_positions:
        if not position_filter(position):
            continue
        contribution = position.position_weight_pct
        if contribution > new_limit:
            out.append(
                RegimeTransitionBreach(
                    position_id=position.position_id,
                    rule_id=rule_id,
                    rule_label=metadata.label,
                    current_value=contribution,
                    new_limit_value=new_limit,
                    overage=contribution - new_limit,
                    unit=metadata.unit,
                )
            )
    return out


def _scan_aggregate_rules(
    *,
    risk_budget: RiskBudgetConsumption,
    new_effective_limits: Mapping[str, float],
    rule_metadata: Mapping[str, RuleMetadata],
) -> list[RegimeTransitionBreach]:
    out: list[RegimeTransitionBreach] = []
    for entry in risk_budget.entries:
        if _classify_rule_id(entry.rule_id) != "aggregate":
            continue
        new_limit = _require(new_effective_limits, entry.rule_id, source="new_effective_limits")
        metadata = _require(rule_metadata, entry.rule_id, source="rule_metadata")
        if entry.current_value > new_limit:
            out.append(
                RegimeTransitionBreach(
                    position_id=None,
                    rule_id=entry.rule_id,
                    rule_label=metadata.label,
                    current_value=entry.current_value,
                    new_limit_value=new_limit,
                    overage=entry.current_value - new_limit,
                    unit=metadata.unit,
                )
            )
    return out


def _require[T](mapping: Mapping[str, T], rule_id: str, *, source: str) -> T:
    """Resolve ``rule_id`` from ``mapping`` or raise a ValueError naming the
    missing key and the source mapping (for actionable diagnostics surfacing
    resolver-output drift or rule-metadata gaps)."""
    if rule_id not in mapping:
        msg = f"{source} is missing required rule_id={rule_id!r}"
        raise ValueError(msg)
    return mapping[rule_id]
