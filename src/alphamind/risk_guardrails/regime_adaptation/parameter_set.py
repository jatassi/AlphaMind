"""Active risk parameter set assembler (story 08).

Pure function that produces a fully-populated portfolio-state
``ActiveRiskParameterSet`` from the orchestrator's resolved inputs — the
active regime, the new transition decision (story 04b), the resolved
effective limits (post-interpolation, post-overlay), the active overlays,
and the prior ``ActiveRiskParameterSet`` for the
``parameter_change_flag`` computation.

The assembler bridges the regime-adaptation feature's outputs to the
``ActiveRiskParameterSet`` shape the rest of the system consumes; it does
not perform I/O and does not mutate inputs.

Reading:
``docs/implementation/06-risk-guardrails/regime-adaptation/08-active-risk-parameter-set-assembler.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from alphamind.config.models.overlays import Overlay
from alphamind.config.models.regimes import Regime
from alphamind.portfolio_state.computations.risk_budget import (
    compute_parameter_change_flag,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    RegimeLabel,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    NextTransitionDecision,
    RuleMetadata,
    overlays_to_strings,
)

_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL: Mapping[Regime, RegimeLabel] = MappingProxyType(
    {
        Regime.low_vol: RegimeLabel.LOW_VOL,
        Regime.normal: RegimeLabel.NORMAL,
        Regime.elevated: RegimeLabel.ELEVATED,
        Regime.crisis: RegimeLabel.CRISIS,
    }
)
"""Bridge between the configuration ``Regime`` enum and the portfolio-state
``RegimeLabel`` enum. Single source of truth for the conversion. The
compile-time assertion below catches enum drift if either side gains a
member without updating the table."""

assert set(_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL) == set(Regime), (
    "_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL must cover every Regime member"
)


def assemble_active_risk_parameter_set(
    *,
    next_transition: NextTransitionDecision,
    base_profile_rule_values: Mapping[str, float],
    active_regime_multipliers: Mapping[str, float],
    interpolated_multipliers: Mapping[str, float],
    overlay_multipliers_composed: Mapping[str, float],
    active_overlays: tuple[Overlay, ...],
    rule_metadata: Mapping[str, RuleMetadata],
    prior_parameter_set: ActiveRiskParameterSet | None,
) -> ActiveRiskParameterSet:
    """Assemble an ``ActiveRiskParameterSet`` from resolved per-rule inputs.

    Each entry's ``value`` is the product of the base profile value, the
    interpolated regime multiplier, and the composed overlay multiplier.
    The entry's ``regime_multiplier_applied`` reports the combined
    multiplier (interpolation times overlays) — i.e. ``value / base_value``
    — so consumers can render the total scaling vs. base without needing to
    reassemble it.

    The ``parameter_change_flag`` reuses
    ``compute_parameter_change_flag`` against ``prior_parameter_set``;
    bootstrap (no prior set) yields ``False``.
    """
    if set(interpolated_multipliers) != set(base_profile_rule_values):
        msg = (
            "interpolated_multipliers and base_profile_rule_values must have "
            "identical key sets; symmetric difference="
            f"{sorted(set(interpolated_multipliers).symmetric_difference(base_profile_rule_values))!r}"
        )
        raise ValueError(msg)
    if set(active_regime_multipliers) != set(interpolated_multipliers):
        msg = (
            "active_regime_multipliers and interpolated_multipliers must have "
            "identical key sets; symmetric difference="
            f"{sorted(set(active_regime_multipliers).symmetric_difference(interpolated_multipliers))!r}"
        )
        raise ValueError(msg)
    # Defensive: the orchestrator never trips this branch by construction; guards direct callers.
    extra_overlay_keys = set(overlay_multipliers_composed).difference(interpolated_multipliers)
    if extra_overlay_keys:
        msg = (
            "overlay_multipliers_composed contains rule_ids absent from "
            f"interpolated_multipliers: {sorted(extra_overlay_keys)!r}"
        )
        raise ValueError(msg)
    missing_metadata_keys = set(interpolated_multipliers).difference(rule_metadata)
    if missing_metadata_keys:
        msg = f"rule_metadata is missing entries for rule_ids: {sorted(missing_metadata_keys)!r}"
        raise ValueError(msg)
    if next_transition.active_regime not in _GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL:
        msg = (
            "next_transition.active_regime is not in the regime-to-label "
            f"mapping; got active_regime={next_transition.active_regime!r}"
        )
        raise ValueError(msg)

    tentative = ActiveRiskParameterSet(
        regime_label=_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL[next_transition.active_regime],
        transition_state=next_transition.transition_state,
        transition_invocations_remaining=next_transition.transition_invocations_remaining,
        parameter_change_flag=False,
        entries=tuple(
            _build_entry(
                rule_id=rule_id,
                base_value=base_profile_rule_values[rule_id],
                interpolated_multiplier=interpolated_multipliers[rule_id],
                overlay_multiplier=overlay_multipliers_composed.get(rule_id, 1.0),
                metadata=rule_metadata[rule_id],
            )
            for rule_id in sorted(interpolated_multipliers)
        ),
        active_overlays=overlays_to_strings(active_overlays),
    )
    return tentative.model_copy(
        update={
            "parameter_change_flag": compute_parameter_change_flag(
                current=tentative,
                prior=prior_parameter_set,
            ),
        },
    )


def _build_entry(
    *,
    rule_id: str,
    base_value: float,
    interpolated_multiplier: float,
    overlay_multiplier: float,
    metadata: RuleMetadata,
) -> ActiveRiskParameterEntry:
    """Compose the per-rule ``ActiveRiskParameterEntry``."""
    combined_multiplier = interpolated_multiplier * overlay_multiplier
    return ActiveRiskParameterEntry(
        rule_id=rule_id,
        rule_label=metadata.label,
        value=base_value * combined_multiplier,
        unit=metadata.unit,
        regime_multiplier_applied=combined_multiplier,
        base_value=base_value,
    )
