"""Shared test helpers for the scheduler orchestrator stubs.

Both ``test_orchestrator.py`` and ``test_orchestrator_progress.py`` patch
``_resolve_regime_adaptation_for_invocation`` to a no-op that returns a
canonical bootstrap output; this module hosts the single factory so the
two test files stay aligned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def make_regime_output(*, now: datetime) -> Any:
    """Minimal :class:`RegimeAdaptationOutput` for orchestrator stub paths.

    The orchestrator passes the output straight into the decision pipeline
    (also stubbed); only the dataclass contract has to hold here. ``now``
    is supplied by the caller so the regime-state ``as_of`` matches the
    surrounding test's clock fixture.
    """
    from alphamind._kernel.regime import RegimeLabel, RegimeTransitionState
    from alphamind.config.models.regimes import Regime
    from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
    from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput
    from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationState

    state = RegimeAdaptationState(
        as_of=now.isoformat().replace("+00:00", "Z"),
        invocation_id="inv-stub",
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label=Regime.normal.value,
        distillation_vix_level=0.0,
        regime_skip_emergency=False,
    )
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=ActiveRiskParameterSet(
            regime_label=RegimeLabel.NORMAL,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            parameter_change_flag=False,
            entries=(),
            active_overlays=(),
        ),
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=state,
        audit_log_entries=(),
    )
