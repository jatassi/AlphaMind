"""End-to-end Scenario B — pre-event window overlapping a stress alert (story 10).

A 4-invocation sequence that walks a calendar with one FOMC event and a
funding-stress alert that stays active throughout. The four invocations
sweep the pre-event activator's three observable states (outside window,
T-2 in-window, T-1 final) plus the post-event lift, while the stress
overlay holds steady — testing the orchestrator's overlay-composition
seam in concert with the audit-log activation/deactivation predicates.

Invocation timing aligns the shipped ``market_hours_rolling`` cron
(``30 9,11,13,15 * * mon-fri`` in US/Eastern) against an FOMC at
``2026-04-29 19:00 UTC`` (15:00 ET) on a Wednesday so:

- Inv 1 (now=15:00 UTC, 11:00 ET) — 2 firings before FOMC → outside pre_event window
- Inv 2 (now=16:00 UTC, 12:00 ET) — 1 firing before FOMC → T-2 of windows=2
- Inv 3 (now=18:00 UTC, 14:00 ET) — 0 firings before FOMC → final pre-event firing
- Inv 4 (now=19:30 UTC, 15:30 ET) — FOMC in past → pre_event inactive

Each invocation seeds a ``DistillationCompositeState`` row before the
orchestrator runs so the production ``fetch_composite_alert_state``
helper sees a calibrated funding-stress alert. This drives the full DB
seam for the stress overlay path.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from alphamind.config.models.overlays import EventType, Overlay
from alphamind.config.resolver import LoadedConfig
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.persistence.models import DistillationCompositeState
from alphamind.portfolio_state.records.capital import RiskBudgetConsumption
from alphamind.risk_guardrails.regime_adaptation import (
    EventCalendar,
    EventCalendarEntry,
    RegimeAdaptationInputs,
    RegimeAdaptationOutput,
    RuleMetadata,
    VixBoundaryThresholds,
    fetch_composite_alert_state,
    insert_state,
    resolve_regime_adaptation,
)

# ---------------------------------------------------------------------------
# Scenario B timeline
# ---------------------------------------------------------------------------

_FOMC_TIMESTAMP = datetime(2026, 4, 29, 19, 0, tzinfo=UTC)  # 15:00 ET on Wed.
_FOMC_LABEL = "FOMC announcement"

# Each entry: (invocation_id, now_utc, expected_active_overlays).
_TIMELINE: tuple[tuple[str, datetime, tuple[Overlay, ...]], ...] = (
    (
        "INV-B-01",
        datetime(2026, 4, 29, 15, 0, tzinfo=UTC),
        (Overlay.stress,),
    ),
    (
        "INV-B-02",
        datetime(2026, 4, 29, 16, 0, tzinfo=UTC),
        (Overlay.pre_event, Overlay.stress),
    ),
    (
        "INV-B-03",
        datetime(2026, 4, 29, 18, 0, tzinfo=UTC),
        (Overlay.pre_event, Overlay.stress),
    ),
    (
        "INV-B-04",
        datetime(2026, 4, 29, 19, 30, tzinfo=UTC),
        (Overlay.stress,),
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vix_thresholds() -> VixBoundaryThresholds:
    return VixBoundaryThresholds(
        low_vol_vix_max=14.0,
        normal_vix_max=22.0,
        elevated_vix_max=35.0,
    )


def _event_calendar() -> EventCalendar:
    return EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=_FOMC_TIMESTAMP,
                label=_FOMC_LABEL,
            ),
        )
    )


def _seed_funding_stress_alert(session: Session, *, as_of: str) -> None:
    """Insert a calibrated funding-stress alert row into the session.

    The orchestrator's stress-activator path consumes the
    ``CompositeAlertState`` value the test passes through inputs; this
    helper exists so tests can also verify the full DB read seam by
    calling ``fetch_composite_alert_state(session)`` and confirming the
    returned record reads the row back correctly.
    """
    session.add(
        DistillationCompositeState(
            composite_kind="funding_stress",
            as_of=as_of,
            composite_value=1.0,
            component_breakdown_json="{}",
            percentile_60d=99.0,
            alert_active=1,
            calibration_state=CalibrationState.CALIBRATED.value,
            ingested_at=as_of,
        )
    )
    session.commit()


def _build_inputs(
    *,
    now_utc: datetime,
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
    session: Session,
) -> RegimeAdaptationInputs:
    composite_alert_state = fetch_composite_alert_state(session)
    return RegimeAdaptationInputs(
        distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
        distillation_vix_level=20.0,
        distillation_regime_skip_emergency=False,
        vix_thresholds=_vix_thresholds(),
        held_positions=(),
        risk_budget=RiskBudgetConsumption(entries=()),
        prior_parameter_set=None,
        event_calendar=_event_calendar(),
        composite_alert_state=composite_alert_state,
        loaded_config=loaded_config,
        rule_metadata=rule_metadata,
    )


def _drive_one_step(
    *,
    invocation_id: str,
    now_utc: datetime,
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
    session: Session,
) -> RegimeAdaptationOutput:
    inputs = _build_inputs(
        now_utc=now_utc,
        loaded_config=loaded_config,
        rule_metadata=rule_metadata,
        session=session,
    )
    output = resolve_regime_adaptation(
        invocation_id=invocation_id,
        now_utc=now_utc,
        inputs=inputs,
        session=session,
    )
    insert_state(
        session,
        output.new_persisted_state,
        ingested_at=now_utc.isoformat(),
    )
    return output


def _drive_full_timeline(
    *,
    in_memory_session: Session,
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
) -> tuple[RegimeAdaptationOutput, ...]:
    """Seed the funding-stress alert and drive all four invocations."""
    _seed_funding_stress_alert(in_memory_session, as_of="2026-04-29T14:00:00Z")
    outputs: list[RegimeAdaptationOutput] = []
    for invocation_id, now_utc, _ in _TIMELINE:
        outputs.append(
            _drive_one_step(
                invocation_id=invocation_id,
                now_utc=now_utc,
                loaded_config=loaded_config,
                rule_metadata=rule_metadata,
                session=in_memory_session,
            )
        )
    return tuple(outputs)


# ---------------------------------------------------------------------------
# Test 1 — active_overlays composition across the timeline
# ---------------------------------------------------------------------------


def test_overlay_composition_matches_expected_per_step(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """Each invocation's ``runtime_dimensions_active_overlays`` matches the table.

    Confirms the orchestrator composes the pre-event activator's
    timing-driven decision with the stress activator's persistence-driven
    decision into the alphabetically-sorted overlay tuple downstream
    consumers expect.
    """
    outputs = _drive_full_timeline(
        in_memory_session=in_memory_session,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
    )
    for output, (invocation_id, _now_utc, expected_overlays) in zip(
        outputs, _TIMELINE, strict=True
    ):
        assert output.runtime_dimensions_active_overlays == expected_overlays, (
            f"step {invocation_id}: expected overlays {expected_overlays}, "
            f"got {output.runtime_dimensions_active_overlays}"
        )


# ---------------------------------------------------------------------------
# Test 2 — pre_event_block_new_positions surfaces on the T-1 invocation
# ---------------------------------------------------------------------------


def test_pre_event_block_new_positions_flag_propagates_through_output(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """``pre_event_block_new_positions`` is True only on the final pre-event step.

    The orchestrator's ``overlay_activation_decisions`` field surfaces
    each activator's per-overlay decision so consumers (e.g., the
    proposal pre-processor) can read the auxiliary flag without
    re-running the activator. Inv 3 is the final pre-event firing
    (zero scheduler firings remain before the FOMC), so its pre-event
    decision sets ``pre_event_block_new_positions=True``; every other
    invocation has it False.
    """
    outputs = _drive_full_timeline(
        in_memory_session=in_memory_session,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
    )
    block_per_step: dict[str, bool] = {}
    for output, (invocation_id, _now_utc, _expected) in zip(outputs, _TIMELINE, strict=True):
        pre_event_decision = next(
            decision
            for decision in output.overlay_activation_decisions
            if decision.overlay == Overlay.pre_event
        )
        block_per_step[invocation_id] = pre_event_decision.pre_event_block_new_positions

    assert block_per_step == {
        "INV-B-01": False,
        "INV-B-02": False,
        "INV-B-03": True,
        "INV-B-04": False,
    }


# ---------------------------------------------------------------------------
# Test 3 — overlay multipliers compound correctly into effective_limits
# ---------------------------------------------------------------------------


def test_effective_limits_compound_overlays_per_rule(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """Per-rule effective limits multiply only the multipliers each overlay touches.

    Pre-event touches ``position_max_size_pct`` only; stress touches
    ``sector_concentration_pct`` (and several other exposure rules) only.
    The two never compound on the same rule under this scenario, but
    each rule sees its own overlay's multiplier when the overlay is
    active. We assert two rules:

    - ``position_max_size_pct``: pre_event=0.80 on inv 2/3; 1.0 elsewhere.
    - ``sector_concentration_pct``: stress=0.85 throughout.

    The base values come from the shipped active profile; the regime
    multiplier is 1.0 since VIX=20 sits in the normal band.
    """
    outputs = _drive_full_timeline(
        in_memory_session=in_memory_session,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
    )
    profile = loaded_config_micro_normal.profiles[loaded_config_micro_normal.main.active_profile]
    base_position_max = profile.rule_values["position_max_size_pct"]
    base_sector_conc = profile.rule_values["sector_concentration_pct"]

    expected_position_max = {
        "INV-B-01": base_position_max * 1.0 * 1.0,  # no pre_event
        "INV-B-02": base_position_max * 1.0 * 0.80,  # pre_event active
        "INV-B-03": base_position_max * 1.0 * 0.80,  # pre_event active
        "INV-B-04": base_position_max * 1.0 * 1.0,  # post-event
    }
    # Stress applies its 0.85 multiplier to sector_concentration_pct on
    # every invocation; pre_event does not touch this rule.
    expected_sector_conc = base_sector_conc * 1.0 * 0.85
    for output, (invocation_id, _now_utc, _expected) in zip(outputs, _TIMELINE, strict=True):
        assert (
            output.effective_limits["position_max_size_pct"] == expected_position_max[invocation_id]
        ), (
            f"step {invocation_id}: expected position_max_size_pct="
            f"{expected_position_max[invocation_id]}, got "
            f"{output.effective_limits['position_max_size_pct']}"
        )
        assert output.effective_limits["sector_concentration_pct"] == expected_sector_conc, (
            f"step {invocation_id}: expected sector_concentration_pct="
            f"{expected_sector_conc}, got "
            f"{output.effective_limits['sector_concentration_pct']}"
        )


# ---------------------------------------------------------------------------
# Test 4 — overlay_activated audit emission predicates
# ---------------------------------------------------------------------------


def test_overlay_activation_audit_entries_emit_on_state_changes(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """Activation/deactivation audits track the active-set delta, not steady state.

    Expected per-step:

    - Inv 1: stress activates for the first time → ``overlay_activated`` for stress.
    - Inv 2: pre_event activates; stress remains active → ``overlay_activated`` for pre_event only.
    - Inv 3: no activation/deactivation → no overlay-change audit entries.
    - Inv 4: pre_event deactivates; stress remains active → ``overlay_deactivated`` for pre_event.
    """
    outputs = _drive_full_timeline(
        in_memory_session=in_memory_session,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
    )
    activations_per_step = _collect_audit_payloads(outputs=outputs, event_kind="overlay_activated")
    deactivations_per_step = _collect_audit_payloads(
        outputs=outputs, event_kind="overlay_deactivated"
    )

    assert _overlays_in(activations_per_step["INV-B-01"]) == (Overlay.stress,)
    assert deactivations_per_step["INV-B-01"] == ()

    assert _overlays_in(activations_per_step["INV-B-02"]) == (Overlay.pre_event,)
    assert deactivations_per_step["INV-B-02"] == ()

    assert activations_per_step["INV-B-03"] == ()
    assert deactivations_per_step["INV-B-03"] == ()

    assert activations_per_step["INV-B-04"] == ()
    assert _overlays_in(deactivations_per_step["INV-B-04"]) == (Overlay.pre_event,)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_audit_payloads(
    *,
    outputs: tuple[RegimeAdaptationOutput, ...],
    event_kind: str,
) -> dict[str, tuple[Mapping[str, object], ...]]:
    """Index by invocation id the audit-entry payloads matching ``event_kind``."""
    return {
        invocation_id: tuple(
            entry.payload for entry in output.audit_log_entries if entry.event_kind == event_kind
        )
        for output, (invocation_id, _now_utc, _expected) in zip(outputs, _TIMELINE, strict=True)
    }


def _overlays_in(payloads: tuple[Mapping[str, object], ...]) -> tuple[Overlay, ...]:
    """Extract the ``overlay`` field from each payload as an ``Overlay`` tuple."""
    overlays: list[Overlay] = []
    for payload in payloads:
        overlay = payload["overlay"]
        assert isinstance(overlay, Overlay)
        overlays.append(overlay)
    return tuple(overlays)
