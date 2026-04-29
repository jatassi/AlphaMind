"""Top-level regime-adaptation orchestrator (story 09).

The pure-with-DB-read function that composes every prior story's primitive
into a single ``RegimeAdaptationOutput``. The orchestrator's only I/O is
``select_most_recent_state(session)``; persistence and config-resolver
invocation are the *caller's* responsibility (the pipeline runtime).

Pipeline:

1. Read prior persisted state.
2. Map distillation regime to guardrail regime.
3. Compute the next transition decision.
4. Resolve active overlays (pre-event + stress).
5. Resolve interpolated multipliers.
6. Compose overlay multipliers.
7. Resolve effective limits.
8. Detect regime-transition breaches.
9. Assemble the active risk parameter set.
10. Construct the new persisted state.
11. Emit audit-log entries.
12. Return the bundled output.

Reading:
``docs/implementation/06-risk-guardrails/regime-adaptation/09-resolve-regime-adaptation-orchestrator.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from sqlalchemy.orm import Session

from alphamind.config.models.overlays import (
    Overlay,
    PreEventOverlay,
    StressOverlay,
)
from alphamind.config.resolver import LoadedConfig
from alphamind.risk_guardrails.regime_adaptation.breach_detector import (
    detect_regime_transition_breaches,
)
from alphamind.risk_guardrails.regime_adaptation.event_calendar import warn_on_stale_calendar
from alphamind.risk_guardrails.regime_adaptation.interpolation import resolve_active_multipliers
from alphamind.risk_guardrails.regime_adaptation.parameter_set import (
    assemble_active_risk_parameter_set,
)
from alphamind.risk_guardrails.regime_adaptation.persistence import select_most_recent_state
from alphamind.risk_guardrails.regime_adaptation.pre_event_activator import (
    evaluate_pre_event_overlay,
)
from alphamind.risk_guardrails.regime_adaptation.regime_mapping import (
    map_distillation_to_guardrail_regime,
)
from alphamind.risk_guardrails.regime_adaptation.stress_activator import evaluate_stress_overlay
from alphamind.risk_guardrails.regime_adaptation.transition_machine import (
    compute_next_transition,
    regime_ladder_index,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    CompositeAlertState,
    EventCalendar,
    NextTransitionDecision,
    OverlayActivationDecision,
    RegimeAdaptationAuditEntry,
    RegimeAdaptationInputs,
    RegimeAdaptationOutput,
    RegimeAdaptationState,
)

# Audit-log event kinds emitted by the orchestrator. The closed vocabulary lives
# here (the only producer); the activity-log table CHECK constraint will mirror
# these strings when the table ships per the story file's Notes.
_EVENT_REGIME_TRANSITION = "regime_transition"
_EVENT_OVERLAY_ACTIVATED = "overlay_activated"
_EVENT_OVERLAY_DEACTIVATED = "overlay_deactivated"
_EVENT_REGIME_SKIP_EMERGENCY = "regime_skip_emergency"
_EVENT_STALE_EVENT_CALENDAR = "stale_event_calendar"


def resolve_regime_adaptation(
    *,
    invocation_id: str,
    now_utc: datetime,
    inputs: RegimeAdaptationInputs,
    session: Session,
) -> RegimeAdaptationOutput:
    """Compose the regime-adaptation feature's per-invocation output.

    The orchestrator is pure-with-DB-read: ``select_most_recent_state(session)``
    is the only I/O. Persistence of the new state, the config resolver
    (``compose_config``), and the activity-log persistence are the caller's
    responsibility.
    """
    prior_state = select_most_recent_state(session)

    new_regime = map_distillation_to_guardrail_regime(
        distillation_label=inputs.distillation_regime_label,
        vix_level=inputs.distillation_vix_level,
        vix_thresholds=inputs.vix_thresholds,
    )

    next_transition = compute_next_transition(
        new_regime=new_regime,
        invocation_id=invocation_id,
        prior_state=prior_state,
    )

    pre_event_decision, stress_decision = _evaluate_overlays(
        now_utc=now_utc,
        loaded_config=inputs.loaded_config,
        event_calendar=inputs.event_calendar,
        composite_alert_state=inputs.composite_alert_state,
    )
    overlay_decisions = (pre_event_decision, stress_decision)
    active_overlays = _active_overlays_sorted(overlay_decisions)

    active_profile_config = inputs.loaded_config.profiles[inputs.loaded_config.main.active_profile]
    base_rule_values = active_profile_config.rule_values
    active_regime_multipliers = inputs.loaded_config.regimes[
        next_transition.active_regime
    ].multipliers
    transition_origin_multipliers = (
        inputs.loaded_config.regimes[next_transition.transition_origin_regime].multipliers
        if next_transition.transition_origin_regime is not None
        else None
    )
    interpolated_multipliers = resolve_active_multipliers(
        transition_state=next_transition.transition_state,
        transition_invocations_remaining=next_transition.transition_invocations_remaining,
        active_regime_multipliers=active_regime_multipliers,
        transition_origin_multipliers=transition_origin_multipliers,
    )

    overlay_multipliers_composed = _compose_overlay_multipliers(
        rule_ids=interpolated_multipliers,
        active_overlays=active_overlays,
        loaded_config=inputs.loaded_config,
    )

    effective_limits = MappingProxyType(
        {
            rule_id: base_rule_values[rule_id]
            * interpolated_multipliers[rule_id]
            * overlay_multipliers_composed[rule_id]
            for rule_id in interpolated_multipliers
        }
    )

    breaches = detect_regime_transition_breaches(
        held_positions=inputs.held_positions,
        risk_budget=inputs.risk_budget,
        new_effective_limits=effective_limits,
        transition_state=next_transition.transition_state,
        rule_metadata=inputs.rule_metadata,
    )

    active_risk_parameter_set = assemble_active_risk_parameter_set(
        next_transition=next_transition,
        base_profile_rule_values=base_rule_values,
        active_regime_multipliers=active_regime_multipliers,
        interpolated_multipliers=interpolated_multipliers,
        overlay_multipliers_composed=overlay_multipliers_composed,
        active_overlays=active_overlays,
        rule_metadata=inputs.rule_metadata,
        prior_parameter_set=inputs.prior_parameter_set,
    )

    new_persisted_state = RegimeAdaptationState(
        as_of=now_utc.isoformat(),
        invocation_id=invocation_id,
        active_regime=next_transition.active_regime,
        prior_regime=next_transition.prior_regime,
        transition_state=next_transition.transition_state,
        transition_invocations_remaining=next_transition.transition_invocations_remaining,
        transition_started_invocation_id=next_transition.transition_started_invocation_id,
        transition_origin_regime=next_transition.transition_origin_regime,
        active_overlays=active_overlays,
        distillation_regime_label=inputs.distillation_regime_label.value,
        distillation_vix_level=inputs.distillation_vix_level,
        regime_skip_emergency=inputs.distillation_regime_skip_emergency,
    )

    audit_log_entries = _emit_audit_log(
        prior_state=prior_state,
        next_transition=next_transition,
        active_regime_multipliers=active_regime_multipliers,
        overlay_decisions=overlay_decisions,
        inputs=inputs,
        now_utc=now_utc,
    )

    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=next_transition.active_regime,
        runtime_dimensions_active_overlays=active_overlays,
        effective_limits=effective_limits,
        active_risk_parameter_set=active_risk_parameter_set,
        regime_transition_breaches=breaches,
        regime_skip_emergency=inputs.distillation_regime_skip_emergency,
        new_persisted_state=new_persisted_state,
        audit_log_entries=audit_log_entries,
    )


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


def _evaluate_overlays(
    *,
    now_utc: datetime,
    loaded_config: LoadedConfig,
    event_calendar: EventCalendar,
    composite_alert_state: CompositeAlertState,
) -> tuple[OverlayActivationDecision, OverlayActivationDecision]:
    """Run both overlay activators and return their decisions in a fixed order."""
    pre_event_overlay = loaded_config.overlays[Overlay.pre_event]
    stress_overlay = loaded_config.overlays[Overlay.stress]
    if not isinstance(pre_event_overlay, PreEventOverlay):
        msg = "loaded_config.overlays[Overlay.pre_event] must be a PreEventOverlay"
        raise TypeError(msg)
    if not isinstance(stress_overlay, StressOverlay):
        msg = "loaded_config.overlays[Overlay.stress] must be a StressOverlay"
        raise TypeError(msg)
    pre_event_decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=event_calendar,
        scheduler_config=loaded_config.scheduler,
        pre_event_overlay=pre_event_overlay,
    )
    stress_decision = evaluate_stress_overlay(
        funding_stress_alert_active=composite_alert_state.funding_stress_alert_active,
        market_liquidity_alert_active=composite_alert_state.market_liquidity_alert_active,
        funding_stress_calibration_state=composite_alert_state.funding_stress_calibration_state,
        market_liquidity_calibration_state=composite_alert_state.market_liquidity_calibration_state,
        stress_overlay=stress_overlay,
    )
    return pre_event_decision, stress_decision


def _active_overlays_sorted(
    decisions: tuple[OverlayActivationDecision, ...],
) -> tuple[Overlay, ...]:
    """Return the active overlays sorted alphabetically by enum value.

    ``Overlay`` is a :class:`StrEnum` so member-level ordering matches value-level
    ordering; no explicit ``key`` is needed.
    """
    return tuple(sorted(d.overlay for d in decisions if d.is_active))


def _compose_overlay_multipliers(
    *,
    rule_ids: Mapping[str, float],
    active_overlays: tuple[Overlay, ...],
    loaded_config: LoadedConfig,
) -> Mapping[str, float]:
    """Multiply per-rule overlay multipliers across all active overlays.

    ``rule_ids`` is iterated for keys only; the values are unused (the
    interpolated-multipliers map happens to be the canonical key source so
    the call site avoids a second container).
    """
    overlay_multiplier_maps = tuple(
        loaded_config.overlays[overlay].multipliers for overlay in active_overlays
    )
    composed: dict[str, float] = {}
    for rule_id in rule_ids:
        product = 1.0
        for overlay_multipliers in overlay_multiplier_maps:
            product *= overlay_multipliers.get(rule_id, 1.0)
        composed[rule_id] = product
    return MappingProxyType(composed)


# ---------------------------------------------------------------------------
# Audit-log emission
# ---------------------------------------------------------------------------


def _emit_audit_log(
    *,
    prior_state: RegimeAdaptationState | None,
    next_transition: NextTransitionDecision,
    active_regime_multipliers: Mapping[str, float],
    overlay_decisions: tuple[OverlayActivationDecision, ...],
    inputs: RegimeAdaptationInputs,
    now_utc: datetime,
) -> tuple[RegimeAdaptationAuditEntry, ...]:
    """Compose the activity-log entries the orchestrator surfaces to the caller."""
    entries: list[RegimeAdaptationAuditEntry] = []

    transition_entry = _maybe_regime_transition_entry(
        prior_state=prior_state,
        next_transition=next_transition,
        active_regime_multipliers=active_regime_multipliers,
        loaded_config=inputs.loaded_config,
    )
    if transition_entry is not None:
        entries.append(transition_entry)

    entries.extend(_overlay_change_entries(prior_state=prior_state, decisions=overlay_decisions))

    if inputs.distillation_regime_skip_emergency:
        entries.append(_regime_skip_emergency_entry(inputs=inputs, prior_state=prior_state))

    stale_entry = _maybe_stale_calendar_entry(event_calendar=inputs.event_calendar, now_utc=now_utc)
    if stale_entry is not None:
        entries.append(stale_entry)

    return tuple(entries)


def _maybe_regime_transition_entry(
    *,
    prior_state: RegimeAdaptationState | None,
    next_transition: NextTransitionDecision,
    active_regime_multipliers: Mapping[str, float],
    loaded_config: LoadedConfig,
) -> RegimeAdaptationAuditEntry | None:
    """Emit a ``regime_transition`` entry when the active regime changed."""
    if prior_state is None:
        return None
    if next_transition.active_regime == prior_state.active_regime:
        return None

    prior_index = regime_ladder_index(prior_state.active_regime)
    new_index = regime_ladder_index(next_transition.active_regime)
    direction = "tightening" if new_index > prior_index else "loosening"
    prior_multipliers = loaded_config.regimes[prior_state.active_regime].multipliers
    payload: Mapping[str, object] = MappingProxyType(
        {
            "prior_regime": prior_state.active_regime.value,
            "new_regime": next_transition.active_regime.value,
            "direction": direction,
            "prior_multipliers_snapshot": MappingProxyType(dict(prior_multipliers)),
            "new_multipliers_snapshot": MappingProxyType(dict(active_regime_multipliers)),
            "transition_invocations_remaining": next_transition.transition_invocations_remaining,
        }
    )
    return RegimeAdaptationAuditEntry(event_kind=_EVENT_REGIME_TRANSITION, payload=payload)


def _overlay_change_entries(
    *,
    prior_state: RegimeAdaptationState | None,
    decisions: tuple[OverlayActivationDecision, ...],
) -> list[RegimeAdaptationAuditEntry]:
    """Emit one entry per overlay whose active state changed across invocations."""
    prior_active = set(prior_state.active_overlays) if prior_state is not None else set()
    entries: list[RegimeAdaptationAuditEntry] = []
    for decision in decisions:
        was_active = decision.overlay in prior_active
        is_active = decision.is_active
        if is_active and not was_active:
            entries.append(
                RegimeAdaptationAuditEntry(
                    event_kind=_EVENT_OVERLAY_ACTIVATED,
                    payload=MappingProxyType(
                        {"overlay": decision.overlay, "rationale": decision.rationale}
                    ),
                )
            )
        elif was_active and not is_active:
            entries.append(
                RegimeAdaptationAuditEntry(
                    event_kind=_EVENT_OVERLAY_DEACTIVATED,
                    payload=MappingProxyType({"overlay": decision.overlay}),
                )
            )
    return entries


def _regime_skip_emergency_entry(
    *,
    inputs: RegimeAdaptationInputs,
    prior_state: RegimeAdaptationState | None,
) -> RegimeAdaptationAuditEntry:
    """Emit the regime-skip emergency passthrough entry."""
    payload: Mapping[str, object] = MappingProxyType(
        {
            "distillation_regime_label": inputs.distillation_regime_label.value,
            "distillation_vix_level": inputs.distillation_vix_level,
            "prior_distillation_regime_label": (
                prior_state.distillation_regime_label if prior_state is not None else None
            ),
        }
    )
    return RegimeAdaptationAuditEntry(event_kind=_EVENT_REGIME_SKIP_EMERGENCY, payload=payload)


def _maybe_stale_calendar_entry(
    *,
    event_calendar: EventCalendar,
    now_utc: datetime,
) -> RegimeAdaptationAuditEntry | None:
    """Emit a ``stale_event_calendar`` entry when the loader reports staleness."""
    report = warn_on_stale_calendar(calendar=event_calendar, now_utc=now_utc)
    if not report.is_stale:
        return None
    payload: Mapping[str, object] = MappingProxyType(
        {
            "latest_event_timestamp_utc": report.latest_event_timestamp_utc,
            "days_until_latest": report.days_until_latest,
        }
    )
    return RegimeAdaptationAuditEntry(event_kind=_EVENT_STALE_EVENT_CALENDAR, payload=payload)
