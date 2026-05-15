"""Pre-fold ``ActiveRiskParameterSet`` and ``RegimeAdaptationOutput`` shims (ALP-472 lift).

These helpers compose the scheduler-side fold of profile * regime * overlays
* feature-flags rule_values into the records the rest of the system reads.
They lived inline in ``scheduler/orchestrator.py`` until ALP-472; the lift
homes them in the regime-adaptation feature they belong to.

The helpers wrap rule_values directly rather than running
:func:`resolve_regime_adaptation` because the resolver already produced the
folded ``rule_values`` map ``compose_config`` writes onto
``ResolvedConfig``. When the regime-adaptation orchestrator is threaded
through the pipeline scheduler (deferred follow-up), these helpers retire
and ``resolve_regime_adaptation``'s real output flows through.

Per the parameter set contract, each entry's ``rule_label`` mirrors its
``rule_id`` and the unit is a flat ``"pct"`` — the values are not surfaced
anywhere downstream in the current pipeline-scheduler call path (the decision
pipeline only reads ``rule_id`` and ``value`` from the entries).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from alphamind._kernel.regime import RegimeLabel, RegimeTransitionState
from alphamind.config.models.regimes import Regime
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    RegimeAdaptationOutput,
    RegimeAdaptationState,
)

__all__ = [
    "build_active_risk_parameters",
    "build_synthetic_regime_output",
    "load_prior_active_risk_parameters",
]


_REGIME_TO_LABEL: dict[Regime, RegimeLabel] = {
    Regime.low_vol: RegimeLabel.LOW_VOL,
    Regime.normal: RegimeLabel.NORMAL,
    Regime.elevated: RegimeLabel.ELEVATED,
    Regime.crisis: RegimeLabel.CRISIS,
}


def build_active_risk_parameters(
    *,
    rule_values: Mapping[str, float],
    regime: Regime,
) -> ActiveRiskParameterSet:
    """Compose an ``ActiveRiskParameterSet`` from a flat rule-values map.

    The production pipeline normally derives this set through
    ``compose_phase_1_enforcement`` which in turn requires a fully-resolved
    :class:`RegimeAdaptationOutput`. Until the regime-adaptation
    orchestrator is threaded through the pipeline scheduler, callers wrap
    the resolved ``rule_values`` directly — they already carry the
    profile * regime * overlay * feature-flag fold ``compose_config``
    produced, which is what the downstream consumers (halt-state
    computation, repository provider, decision pipeline) actually read.

    Each entry's ``rule_label`` / ``unit`` mirror the ``rule_id`` and a
    flat ``"pct"`` unit — the values aren't surfaced anywhere downstream
    in the current pipeline-scheduler call path (the decision pipeline
    only reads ``rule_id`` and ``value`` from the entries).
    """
    entries = tuple(
        ActiveRiskParameterEntry(
            rule_id=rule_id,
            rule_label=rule_id,
            value=float(value),
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=float(value),
        )
        for rule_id, value in sorted(rule_values.items())
    )
    return ActiveRiskParameterSet(
        regime_label=_REGIME_TO_LABEL[regime],
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


def build_synthetic_regime_output(
    *,
    active_risk_parameters: ActiveRiskParameterSet,
    runtime_active_regime: Regime,
    invocation_id: str,
    now: datetime,
) -> RegimeAdaptationOutput:
    """Wrap a folded ``ActiveRiskParameterSet`` in a synthetic ``RegimeAdaptationOutput``.

    The scheduler does not yet invoke :func:`resolve_regime_adaptation`
    (its rule_values already carry the profile * regime * overlay fold
    ``compose_config`` produces). Story ALP-433 still needs a
    :class:`RegimeAdaptationOutput` to thread into the pipeline's Phase 1
    enforcement composition; this helper wraps the parameter set in an
    otherwise-empty bundle. When the regime-adaptation orchestrator is
    wired into the scheduler (deferred follow-up), this helper retires
    and :func:`resolve_regime_adaptation`'s real output flows through.
    """
    state = RegimeAdaptationState(
        as_of=now.isoformat().replace("+00:00", "Z"),
        invocation_id=invocation_id,
        active_regime=runtime_active_regime,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label=runtime_active_regime.value,
        distillation_vix_level=0.0,
        regime_skip_emergency=False,
    )
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=runtime_active_regime,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=active_risk_parameters,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=state,
        audit_log_entries=(),
    )


def load_prior_active_risk_parameters(snapshot_path: str) -> ActiveRiskParameterSet:
    """Rehydrate an ``ActiveRiskParameterSet`` from a resolved-config snapshot.

    Reads the JSON file persisted by
    :func:`alphamind.config.snapshot.persist_snapshot`, extracts the
    ``rule_values`` map and ``regime_label`` string the prior invocation
    composed, and re-wraps them via :func:`build_active_risk_parameters` so
    the snapshot assembler reads the same values the decision pipeline
    consumed at the time the prior invocation wrote that snapshot.

    Raises :class:`FileNotFoundError` when the snapshot file is missing;
    the fallback to the current parameter set is the repository-provider's
    responsibility, not the loader's. Raises :class:`json.JSONDecodeError`
    on a corrupt snapshot — defensive fallback would mask a contract
    violation by substituting an unrelated set.
    """
    payload = json.loads(Path(snapshot_path).read_text())
    return build_active_risk_parameters(
        rule_values=payload["rule_values"],
        regime=Regime(payload["regime_label"]),
    )
