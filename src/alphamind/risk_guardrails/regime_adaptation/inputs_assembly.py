"""Helpers that assemble :class:`RegimeAdaptationInputs` for the pipeline runtime.

The scheduler and the continuous-monitor substrate both invoke
:func:`resolve_regime_adaptation` per unit of work (per invocation in the
scheduler, per tick in the monitor). The two callers gather the same input
fan from different surfaces: the scheduler reads fresh distillation outputs
from the analysis pipeline result, while the monitor reads them from the
most-recently-persisted ``regime_adaptation_state`` row. This module
centralizes the common construction so neither caller open-codes a
half-dozen field copies.

``build_rule_metadata`` and :class:`RegimeAdaptationConfigFan` are the
two reusable primitives; the per-caller assemblers
(:func:`build_inputs_from_distillation_outputs` for the scheduler,
:func:`build_inputs_from_persisted_state` for the monitor) compose them
with caller-specific fields (distillation values, prior parameter set).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from alphamind.config.resolver import LoadedConfig
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.regime_adaptation.event_calendar import load_event_calendar
from alphamind.risk_guardrails.regime_adaptation.regime_mapping import (
    from_regime_classification,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    CompositeAlertState,
    EventCalendar,
    RegimeAdaptationInputs,
    RegimeAdaptationState,
    RuleMetadata,
    VixBoundaryThresholds,
)

if TYPE_CHECKING:
    from alphamind.config.models.distillation import DistillationConfig

__all__ = [
    "RegimeAdaptationConfigFan",
    "build_inputs_from_distillation_outputs",
    "build_inputs_from_persisted_state",
    "build_rule_metadata",
    "load_config_fan",
]


def build_rule_metadata(loaded_config: LoadedConfig) -> dict[str, RuleMetadata]:
    """Build the rule-metadata map the breach detector consumes.

    The active profile's ``rule_values`` keys are the universe of rule_ids
    the resolver folds; each gets a metadata entry whose ``label`` mirrors
    the ``rule_id`` and whose ``unit`` is the flat ``"pct"`` the existing
    pipeline-scheduler shim used. The current breach-detector consumer
    only reads ``rule_id`` and ``label``; richer per-rule metadata is the
    rules-and-limits feature's job and will land via a later integration.
    """
    active_profile = loaded_config.profiles[loaded_config.main.active_profile]
    return {
        rule_id: RuleMetadata(rule_id=rule_id, label=rule_id, unit="pct")
        for rule_id in active_profile.rule_values
    }


@dataclass(frozen=True, slots=True)
class RegimeAdaptationConfigFan:
    """Slow-changing inputs the resolver consumes — loaded once per process.

    The scheduler reloads this per invocation (config changes require an
    operator restart, so per-invocation load is conservative); the monitor
    loads it once at daemon startup. Bundling here lets both callers share
    one loader and pass one record to the resolver assembler.
    """

    loaded_config: LoadedConfig
    vix_thresholds: VixBoundaryThresholds
    event_calendar: EventCalendar
    rule_metadata: Mapping[str, RuleMetadata]


def load_config_fan(
    *,
    config_dir: Path,
    loaded_config: LoadedConfig,
    distillation_config: DistillationConfig,
) -> RegimeAdaptationConfigFan:
    """Read every config surface the resolver consumes into one bundle.

    ``loaded_config`` and ``distillation_config`` are the caller's
    already-parsed Pydantic bundles; this helper only adds the
    event-calendar load and the VIX-threshold + rule-metadata derivations.
    Splitting these out lets callers reuse their existing config-loading
    machinery rather than re-reading YAMLs.
    """
    return RegimeAdaptationConfigFan(
        loaded_config=loaded_config,
        vix_thresholds=from_regime_classification(distillation_config.regime_classification),
        event_calendar=load_event_calendar(config_dir / "event_calendar.yaml"),
        rule_metadata=build_rule_metadata(loaded_config),
    )


def build_inputs_from_distillation_outputs(
    *,
    fan: RegimeAdaptationConfigFan,
    distillation_regime_label: DistillationRegimeLabel,
    distillation_vix_level: float,
    distillation_regime_skip_emergency: bool,
    held_positions: tuple[PositionView, ...],
    risk_budget: RiskBudgetConsumption,
    prior_parameter_set: ActiveRiskParameterSet | None,
    composite_alert_state: CompositeAlertState,
) -> RegimeAdaptationInputs:
    """Compose :class:`RegimeAdaptationInputs` from the scheduler's surfaces.

    The scheduler reads ``distillation_regime_label`` / ``distillation_vix_level``
    / ``distillation_regime_skip_emergency`` from the fresh
    :class:`DistillationOutputs` the analysis pipeline produced. The monitor
    uses :func:`build_inputs_from_persisted_state` instead — it reads the
    same fields from the most-recently-persisted ``RegimeAdaptationState``.
    """
    return RegimeAdaptationInputs(
        distillation_regime_label=distillation_regime_label,
        distillation_vix_level=distillation_vix_level,
        distillation_regime_skip_emergency=distillation_regime_skip_emergency,
        vix_thresholds=fan.vix_thresholds,
        held_positions=held_positions,
        risk_budget=risk_budget,
        prior_parameter_set=prior_parameter_set,
        event_calendar=fan.event_calendar,
        composite_alert_state=composite_alert_state,
        loaded_config=fan.loaded_config,
        rule_metadata=fan.rule_metadata,
    )


def build_inputs_from_persisted_state(
    *,
    fan: RegimeAdaptationConfigFan,
    persisted_state: RegimeAdaptationState,
    held_positions: tuple[PositionView, ...],
    risk_budget: RiskBudgetConsumption,
    prior_parameter_set: ActiveRiskParameterSet | None,
    composite_alert_state: CompositeAlertState,
) -> RegimeAdaptationInputs:
    """Compose :class:`RegimeAdaptationInputs` from the monitor's surfaces.

    The monitor does not run distillation; it reads the last-persisted
    :class:`RegimeAdaptationState` (which carries the same distillation
    fields the scheduler wrote at its invocation). The ``distillation_*``
    fields on the persisted row are exactly the values the scheduler fed
    to the resolver at that invocation, so feeding them back yields a
    consistent ``RegimeAdaptationOutput`` across ticks within an invocation.
    """
    distillation_label = DistillationRegimeLabel(persisted_state.distillation_regime_label)
    return RegimeAdaptationInputs(
        distillation_regime_label=distillation_label,
        distillation_vix_level=persisted_state.distillation_vix_level,
        distillation_regime_skip_emergency=persisted_state.regime_skip_emergency,
        vix_thresholds=fan.vix_thresholds,
        held_positions=held_positions,
        risk_budget=risk_budget,
        prior_parameter_set=prior_parameter_set,
        event_calendar=fan.event_calendar,
        composite_alert_state=composite_alert_state,
        loaded_config=fan.loaded_config,
        rule_metadata=fan.rule_metadata,
    )
