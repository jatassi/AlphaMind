"""Pre-fold ``ActiveRiskParameterSet`` building block (ALP-472 lift; ALP-513 retire).

:func:`build_active_risk_parameters` wraps a flat ``rule_values`` map into an
:class:`ActiveRiskParameterSet`. Two production callers remain after ALP-513:

* The scheduler's pre-runtime halt-state check, which needs the
  ``daily_drawdown_pct`` value before the regime-adaptation orchestrator can
  run (the resolver's inputs themselves depend on a halt-state probe).
* The scheduler's snapshot-assembly parameter-set seed, which feeds the
  ``ActiveRiskParameterSet`` field on :class:`PortfolioStateSnapshot` — that
  field is informational for downstream consumers and does not need the
  resolver's transition / overlay bookkeeping.

The companion ``build_synthetic_regime_output`` shim retired in ALP-513;
``resolve_regime_adaptation``'s real output now flows through every
:func:`compose_phase_1_enforcement` call site.

Per the parameter set contract, each entry's ``rule_label`` mirrors its
``rule_id`` and the unit is a flat ``"pct"`` — the values are not surfaced
anywhere downstream in the current pipeline-scheduler call path (the decision
pipeline only reads ``rule_id`` and ``value`` from the entries).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from alphamind._kernel.regime import RegimeLabel, RegimeTransitionState
from alphamind.config.models.regimes import Regime
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)

__all__ = [
    "build_active_risk_parameters",
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

    The canonical pipeline path is :func:`resolve_regime_adaptation` →
    :func:`compose_phase_1_enforcement`. This helper exists for callers
    that need a parameter set *before* the resolver can run (the
    scheduler's pre-runtime halt-state probe, the snapshot's seed
    parameter set, the :func:`load_prior_active_risk_parameters`
    rehydration). Each such caller takes the already-folded
    ``rule_values`` ``compose_config`` produced and wraps it without
    overlay / transition / parameter-change-flag bookkeeping — those
    fields are the resolver's contribution and are unused by this
    helper's consumers.

    Each entry's ``rule_label`` / ``unit`` mirror the ``rule_id`` and a
    flat ``"pct"`` unit.
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
    payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    return build_active_risk_parameters(
        rule_values=payload["rule_values"],
        regime=Regime(payload["regime_label"]),
    )
