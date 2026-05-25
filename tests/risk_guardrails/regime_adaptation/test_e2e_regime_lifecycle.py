"""End-to-end Scenarios A + C for the regime-adaptation orchestrator (story 10).

**Scenario A** — calm-week → crisis-week → recovery-week. A 10-invocation
sequence walking the full transition lifecycle: low_vol stable, tightening
into normal, stabilizing, tightening into crisis, stabilizing, then
loosening down through elevated for three invocations before stabilizing.

**Scenario C** — regime-skip emergency passthrough. A 2-invocation
sequence: a calm low-vol baseline followed by a CRISIS_SPIKE marked as a
regime-skip emergency by the upstream distillation classifier. The
orchestrator must forward the flag verbatim, emit a
``regime_skip_emergency`` audit entry, and also emit the
``regime_transition`` audit since the ladder index moved.

Each invocation:

1. Builds a fresh ``RegimeAdaptationInputs`` describing the upstream
   distillation observation (regime label + VIX, optionally with the
   regime-skip emergency flag set).
2. Calls ``resolve_regime_adaptation`` with the running session.
3. Asserts the per-invocation expected output (regime, transition state,
   transition countdown, audit log composition, interpolated effective
   limits).
4. Persists the new state via ``insert_state`` so the next invocation's
   ``select_most_recent_state`` reads it back — driving the full
   read-compute-persist seam.

Per the story file, these scenarios are the regime-adaptation work tree's
seam tests for the regime-classification, transition-mechanics,
breach-detection, and emergency-passthrough composition. Per-primitive
correctness is covered in stories 03-08; this story confirms they
integrate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from alphamind._kernel.regime import RegimeTransitionState
from alphamind.config.models.regimes import Regime
from alphamind.config.resolver import LoadedConfig
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.regime_adaptation import (
    CompositeAlertState,
    EventCalendar,
    RegimeAdaptationInputs,
    RegimeAdaptationOutput,
    RuleMetadata,
    VixBoundaryThresholds,
    insert_state,
    resolve_regime_adaptation,
    select_most_recent_state,
)

# ---------------------------------------------------------------------------
# Scenario A timeline
# ---------------------------------------------------------------------------
# Reference start instant. The orchestrator's clock-driven primitives
# (event-calendar staleness, pre-event activator) are decoupled here by
# passing an empty calendar; we advance the clock 4 hours per invocation
# so each persisted ``as_of`` is unique and sortable.
_T0 = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
_PER_INVOCATION_DELTA = timedelta(hours=4)


@dataclass(frozen=True)
class _Step:
    """One scripted invocation in a scenario timeline.

    The expected fields are the story file's table columns; the test
    asserts the orchestrator's output against them.
    """

    invocation_id: str
    distillation_regime_label: DistillationRegimeLabel
    distillation_vix_level: float
    expected_active_regime: Regime
    expected_transition_state: RegimeTransitionState
    expected_transition_invocations_remaining: int
    expected_emits_regime_transition_audit: bool
    distillation_regime_skip_emergency: bool = False


# Scenario A's 10-step timeline. The first six invocations exercise the
# tightening branch (low_vol → normal → crisis) and stable continuations;
# invocations 7-10 exercise the loosening branch (crisis → elevated)
# including the linear-interpolation countdown and the STABLE arrival.
#
# The story file's invocation-10 row lists VIX=22, but the underlying
# distillation classifier maps VIX <= 22 to the NORMAL band (i.e.
# Regime.normal), not Regime.elevated. We use VIX=23 here so the regime
# stays ``elevated`` through the final LOOSENING completion step, matching
# the story's narrative intent (the regime ladder index should not move
# during the loosening countdown). The other numeric values match the
# story file verbatim.
_TIMELINE: tuple[_Step, ...] = (
    _Step(
        invocation_id="INV-A-01",
        distillation_regime_label=DistillationRegimeLabel.LOW_VOL_COMPRESSION,
        distillation_vix_level=12.0,
        expected_active_regime=Regime.low_vol,
        expected_transition_state=RegimeTransitionState.STABLE,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=False,
    ),
    _Step(
        invocation_id="INV-A-02",
        distillation_regime_label=DistillationRegimeLabel.LOW_VOL_COMPRESSION,
        distillation_vix_level=13.0,
        expected_active_regime=Regime.low_vol,
        expected_transition_state=RegimeTransitionState.STABLE,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=False,
    ),
    _Step(
        invocation_id="INV-A-03",
        distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
        distillation_vix_level=18.0,
        expected_active_regime=Regime.normal,
        expected_transition_state=RegimeTransitionState.TIGHTENING,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=True,
    ),
    _Step(
        invocation_id="INV-A-04",
        distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
        distillation_vix_level=19.0,
        expected_active_regime=Regime.normal,
        expected_transition_state=RegimeTransitionState.STABLE,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=False,
    ),
    _Step(
        invocation_id="INV-A-05",
        distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
        distillation_vix_level=42.0,
        expected_active_regime=Regime.crisis,
        expected_transition_state=RegimeTransitionState.TIGHTENING,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=True,
    ),
    _Step(
        invocation_id="INV-A-06",
        distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
        distillation_vix_level=38.0,
        expected_active_regime=Regime.crisis,
        expected_transition_state=RegimeTransitionState.STABLE,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=False,
    ),
    _Step(
        invocation_id="INV-A-07",
        distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
        distillation_vix_level=25.0,
        expected_active_regime=Regime.elevated,
        expected_transition_state=RegimeTransitionState.LOOSENING,
        expected_transition_invocations_remaining=3,
        expected_emits_regime_transition_audit=True,
    ),
    _Step(
        invocation_id="INV-A-08",
        distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
        distillation_vix_level=24.0,
        expected_active_regime=Regime.elevated,
        expected_transition_state=RegimeTransitionState.LOOSENING,
        expected_transition_invocations_remaining=2,
        expected_emits_regime_transition_audit=False,
    ),
    _Step(
        invocation_id="INV-A-09",
        distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
        distillation_vix_level=23.0,
        expected_active_regime=Regime.elevated,
        expected_transition_state=RegimeTransitionState.LOOSENING,
        expected_transition_invocations_remaining=1,
        expected_emits_regime_transition_audit=False,
    ),
    _Step(
        invocation_id="INV-A-10",
        distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
        distillation_vix_level=23.0,
        expected_active_regime=Regime.elevated,
        expected_transition_state=RegimeTransitionState.STABLE,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=False,
    ),
)


# Scenario C — regime-skip emergency passthrough. Two invocations: a calm
# low-vol baseline followed by a CRISIS_SPIKE marked as a regime-skip
# emergency by the upstream distillation classifier. The orchestrator
# must forward the flag and emit both ``regime_transition`` and
# ``regime_skip_emergency`` audits on the emergency invocation.
_SCENARIO_C_TIMELINE: tuple[_Step, ...] = (
    _Step(
        invocation_id="INV-C-01",
        distillation_regime_label=DistillationRegimeLabel.LOW_VOL_COMPRESSION,
        distillation_vix_level=12.0,
        expected_active_regime=Regime.low_vol,
        expected_transition_state=RegimeTransitionState.STABLE,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=False,
        distillation_regime_skip_emergency=False,
    ),
    _Step(
        invocation_id="INV-C-02",
        distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
        distillation_vix_level=50.0,
        expected_active_regime=Regime.crisis,
        expected_transition_state=RegimeTransitionState.TIGHTENING,
        expected_transition_invocations_remaining=0,
        expected_emits_regime_transition_audit=True,
        distillation_regime_skip_emergency=True,
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vix_thresholds() -> VixBoundaryThresholds:
    """The shipped distillation VIX-band thresholds.

    Mirrors ``config/distillation.yaml``'s
    ``regime_classification.regime_*_vix_max`` values; hard-coded here so
    the test does not couple to that file's loader.
    """
    return VixBoundaryThresholds(
        low_vol_vix_max=14.0,
        normal_vix_max=22.0,
        elevated_vix_max=35.0,
    )


def _quiet_alert_state() -> CompositeAlertState:
    """No funding-stress or market-liquidity alerts; both calibrated."""
    return CompositeAlertState(
        funding_stress_alert_active=False,
        funding_stress_calibration_state=CalibrationState.CALIBRATED,
        funding_stress_as_of=None,
        market_liquidity_alert_active=False,
        market_liquidity_calibration_state=CalibrationState.CALIBRATED,
        market_liquidity_as_of=None,
    )


def _empty_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _build_inputs(
    *,
    step: _Step,
    held_positions: tuple[PositionView, ...],
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
) -> RegimeAdaptationInputs:
    return RegimeAdaptationInputs(
        distillation_regime_label=step.distillation_regime_label,
        distillation_vix_level=step.distillation_vix_level,
        distillation_regime_skip_emergency=step.distillation_regime_skip_emergency,
        vix_thresholds=_vix_thresholds(),
        held_positions=held_positions,
        risk_budget=_empty_risk_budget(),
        prior_parameter_set=None,
        event_calendar=EventCalendar(entries=()),
        composite_alert_state=_quiet_alert_state(),
        loaded_config=loaded_config,
        rule_metadata=rule_metadata,
    )


def _drive_one_step(
    *,
    step: _Step,
    now_utc: datetime,
    held_positions: tuple[PositionView, ...],
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
    session: Session,
) -> RegimeAdaptationOutput:
    inputs = _build_inputs(
        step=step,
        held_positions=held_positions,
        loaded_config=loaded_config,
        rule_metadata=rule_metadata,
    )
    output = resolve_regime_adaptation(
        invocation_id=step.invocation_id,
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


# ---------------------------------------------------------------------------
# Test 1 — full timeline composition
# ---------------------------------------------------------------------------


def test_regime_lifecycle_drives_every_transition_and_audit_correctly(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """The full 10-step lifecycle composes every primitive without drift.

    Walks the timeline once, asserting per-step:

    - ``runtime_dimensions_active_regime`` matches the expected regime.
    - ``new_persisted_state.transition_state`` and
      ``transition_invocations_remaining`` match the expected values.
    - The ``regime_transition`` audit entry fires only on the steps marked
      ``expected_emits_regime_transition_audit`` (fresh transitions).

    The persisted state from each step feeds the next step's
    ``select_most_recent_state`` lookup, so the test exercises the
    full read-compute-persist seam — not just the orchestrator's pure
    composition.
    """
    held_positions: tuple[PositionView, ...] = ()
    for index, step in enumerate(_TIMELINE):
        now_utc = _T0 + index * _PER_INVOCATION_DELTA
        output = _drive_one_step(
            step=step,
            now_utc=now_utc,
            held_positions=held_positions,
            loaded_config=loaded_config_micro_normal,
            rule_metadata=rule_metadata_from_shipped_registry,
            session=in_memory_session,
        )

        assert output.runtime_dimensions_active_regime == step.expected_active_regime, (
            f"step {step.invocation_id}: expected regime "
            f"{step.expected_active_regime}, got {output.runtime_dimensions_active_regime}"
        )
        assert output.new_persisted_state.transition_state == step.expected_transition_state, (
            f"step {step.invocation_id}: expected transition_state "
            f"{step.expected_transition_state}, got "
            f"{output.new_persisted_state.transition_state}"
        )
        assert (
            output.new_persisted_state.transition_invocations_remaining
            == step.expected_transition_invocations_remaining
        ), (
            f"step {step.invocation_id}: expected remaining "
            f"{step.expected_transition_invocations_remaining}, got "
            f"{output.new_persisted_state.transition_invocations_remaining}"
        )
        emitted_kinds = {entry.event_kind for entry in output.audit_log_entries}
        if step.expected_emits_regime_transition_audit:
            assert "regime_transition" in emitted_kinds, (
                f"step {step.invocation_id}: expected regime_transition audit "
                f"entry, got kinds={emitted_kinds}"
            )
        else:
            assert "regime_transition" not in emitted_kinds, (
                f"step {step.invocation_id}: expected no regime_transition "
                f"audit entry, got kinds={emitted_kinds}"
            )


# ---------------------------------------------------------------------------
# Test 2 — interpolation at LOOSENING steps 7, 8, 9
# ---------------------------------------------------------------------------


def test_loosening_window_interpolates_position_max_size_pct_linearly(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """The three LOOSENING invocations interpolate ``position_max_size_pct``.

    The story file pins the linear interpolation explicitly: invocations
    7, 8, 9 use fractions 1/3, 2/3, 1 (origin=crisis multiplier 0.40,
    destination=elevated multiplier 0.70). The base value for the active
    profile is 5; the test asserts the interpolated effective limits
    against the worked formula

        ``base * (origin + (destination - origin) * fraction)``

    so a refactor that swaps the interpolation formula or the regime
    multipliers is caught here, not in a downstream renderer test.
    """
    fractions = {7: 1.0 / 3.0, 8: 2.0 / 3.0, 9: 1.0}
    base_value = loaded_config_micro_normal.profiles[
        loaded_config_micro_normal.main.active_profile
    ].rule_values["position_max_size_pct"]
    origin_multiplier = loaded_config_micro_normal.regimes[Regime.crisis].multipliers[
        "position_max_size_pct"
    ]
    destination_multiplier = loaded_config_micro_normal.regimes[Regime.elevated].multipliers[
        "position_max_size_pct"
    ]

    captured_position_max: dict[int, float] = {}
    for index, step in enumerate(_TIMELINE):
        now_utc = _T0 + index * _PER_INVOCATION_DELTA
        output = _drive_one_step(
            step=step,
            now_utc=now_utc,
            held_positions=(),
            loaded_config=loaded_config_micro_normal,
            rule_metadata=rule_metadata_from_shipped_registry,
            session=in_memory_session,
        )
        # Step indices are 1-based in the story file.
        if index + 1 in fractions:
            captured_position_max[index + 1] = output.effective_limits["position_max_size_pct"]

    for invocation_index, fraction in fractions.items():
        expected = base_value * (
            origin_multiplier + (destination_multiplier - origin_multiplier) * fraction
        )
        assert captured_position_max[invocation_index] == expected, (
            f"step {invocation_index}: expected interpolated "
            f"position_max_size_pct={expected}, got "
            f"{captured_position_max[invocation_index]}"
        )


# ---------------------------------------------------------------------------
# Test 3 — held position breach on the crisis tightening step
# ---------------------------------------------------------------------------


def test_crisis_tightening_emits_position_max_size_breach(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
    held_position_at_5pct: PositionView,
) -> None:
    """A held 5% position breaches ``position_max_size_pct`` on the crisis step.

    Invocation 5 is the TIGHTENING transition into ``crisis`` whose
    multiplier on ``position_max_size_pct`` is 0.40. With base 5%, the
    new effective limit is 2%; a 5% held position breaches by 3 pp. This
    confirms the orchestrator wires the breach detector against the
    *new* (post-tightening) effective limits, not the prior limits.
    """
    held_positions: tuple[PositionView, ...] = ()
    crisis_breach_seen = False
    for index, step in enumerate(_TIMELINE):
        # Activate the held position as of invocation 5 (the crisis step);
        # earlier invocations run with an empty portfolio per the story
        # file's "scenario A's first 6 invocations run with held_positions=()"
        # contract for the no-breach path.
        if step.invocation_id == "INV-A-05":
            held_positions = (held_position_at_5pct,)
        now_utc = _T0 + index * _PER_INVOCATION_DELTA
        output = _drive_one_step(
            step=step,
            now_utc=now_utc,
            held_positions=held_positions,
            loaded_config=loaded_config_micro_normal,
            rule_metadata=rule_metadata_from_shipped_registry,
            session=in_memory_session,
        )
        if step.invocation_id == "INV-A-05":
            position_breaches = [
                breach
                for breach in output.regime_transition_breaches
                if breach.rule_id == "position_max_size_pct"
                and breach.position_id == held_position_at_5pct.position_id
            ]
            assert len(position_breaches) == 1, (
                f"expected exactly one position_max_size_pct breach for the "
                f"5% held position on the crisis tightening step; got "
                f"{output.regime_transition_breaches}"
            )
            breach = position_breaches[0]
            # base 5 * crisis 0.40 = 2.0 effective limit; current 5; overage 3.
            assert breach.new_limit_value == 2.0
            assert breach.current_value == 5.0
            assert breach.overage == 3.0
            crisis_breach_seen = True
    assert crisis_breach_seen, "the test did not reach invocation 5"


# ---------------------------------------------------------------------------
# Test 4 — empty-portfolio path produces no breaches across the timeline
# ---------------------------------------------------------------------------


def test_empty_held_positions_produce_no_breaches_across_timeline(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """No held positions, even on tightening steps → no breach records.

    The breach detector treats an empty portfolio as a no-op regardless
    of the transition state; this test confirms the orchestrator
    surfaces the empty tuple without confusing a post-tightening
    aggregate-rule check with a per-position check.
    """
    for index, step in enumerate(_TIMELINE):
        now_utc = _T0 + index * _PER_INVOCATION_DELTA
        output = _drive_one_step(
            step=step,
            now_utc=now_utc,
            held_positions=(),
            loaded_config=loaded_config_micro_normal,
            rule_metadata=rule_metadata_from_shipped_registry,
            session=in_memory_session,
        )
        assert output.regime_transition_breaches == (), (
            f"step {step.invocation_id}: expected no breaches with empty "
            f"portfolio, got {output.regime_transition_breaches}"
        )


# ---------------------------------------------------------------------------
# Test 5 — persistence round-trip after the timeline completes
# ---------------------------------------------------------------------------


def test_persistence_round_trip_preserves_final_state_field_by_field(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """The last persisted state reads back equal to the orchestrator's output.

    Drives the full timeline, captures the final orchestrator output's
    ``new_persisted_state``, then re-reads via
    ``select_most_recent_state`` and asserts equality. Catches a row-to-
    record-adapter regression that would corrupt the next invocation's
    transition lookback.
    """
    last_output: RegimeAdaptationOutput | None = None
    for index, step in enumerate(_TIMELINE):
        now_utc = _T0 + index * _PER_INVOCATION_DELTA
        last_output = _drive_one_step(
            step=step,
            now_utc=now_utc,
            held_positions=(),
            loaded_config=loaded_config_micro_normal,
            rule_metadata=rule_metadata_from_shipped_registry,
            session=in_memory_session,
        )
    assert last_output is not None
    recovered = select_most_recent_state(in_memory_session)
    assert recovered == last_output.new_persisted_state


# ---------------------------------------------------------------------------
# Test 6 — Scenario C: regime-skip emergency flag passthrough + audit
# ---------------------------------------------------------------------------


def test_regime_skip_emergency_flag_passes_through_with_audit_entry(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """Inv 2's ``regime_skip_emergency=True`` surfaces on the output and audit log.

    Inv 1 establishes a calm low-vol baseline (no emergency flag, no
    audit). Inv 2 reports a CRISIS_SPIKE with the emergency flag set;
    the orchestrator forwards the flag and emits a
    ``regime_skip_emergency`` audit entry whose payload includes the
    distillation regime label and VIX level.
    """
    outputs: list[RegimeAdaptationOutput] = []
    for index, step in enumerate(_SCENARIO_C_TIMELINE):
        now_utc = _T0 + index * _PER_INVOCATION_DELTA
        outputs.append(
            _drive_one_step(
                step=step,
                now_utc=now_utc,
                held_positions=(),
                loaded_config=loaded_config_micro_normal,
                rule_metadata=rule_metadata_from_shipped_registry,
                session=in_memory_session,
            )
        )

    output_1, output_2 = outputs
    assert output_1.regime_skip_emergency is False
    inv_1_kinds = {entry.event_kind for entry in output_1.audit_log_entries}
    assert "regime_skip_emergency" not in inv_1_kinds

    # Passthrough on the output record + persisted state.
    assert output_2.regime_skip_emergency is True
    assert output_2.new_persisted_state.regime_skip_emergency is True
    # Active regime resolves to crisis (label-driven, ignores VIX).
    assert output_2.runtime_dimensions_active_regime == Regime.crisis

    # Audit log: one regime_skip_emergency entry with the correct payload.
    skip_entries = [
        entry for entry in output_2.audit_log_entries if entry.event_kind == "regime_skip_emergency"
    ]
    assert len(skip_entries) == 1
    payload = skip_entries[0].payload
    assert payload["distillation_regime_label"] == DistillationRegimeLabel.CRISIS_SPIKE.value
    assert payload["distillation_vix_level"] == 50.0
    assert payload["prior_distillation_regime_label"] == (
        DistillationRegimeLabel.LOW_VOL_COMPRESSION.value
    )


# ---------------------------------------------------------------------------
# Test 7 — Scenario C: regime_transition audit coexists with the skip audit
# ---------------------------------------------------------------------------


def test_regime_skip_emergency_invocation_also_emits_regime_transition_audit(
    in_memory_session: Session,
    loaded_config_micro_normal: LoadedConfig,
    rule_metadata_from_shipped_registry: Mapping[str, RuleMetadata],
) -> None:
    """The emergency invocation crosses the regime ladder; both audits coexist.

    A regime-skip emergency does not suppress the regime-transition
    audit emission — the ladder index moved (low_vol → crisis), so the
    orchestrator emits both ``regime_transition`` and
    ``regime_skip_emergency`` entries on the same invocation.
    """
    output_2: RegimeAdaptationOutput | None = None
    for index, step in enumerate(_SCENARIO_C_TIMELINE):
        now_utc = _T0 + index * _PER_INVOCATION_DELTA
        output_2 = _drive_one_step(
            step=step,
            now_utc=now_utc,
            held_positions=(),
            loaded_config=loaded_config_micro_normal,
            rule_metadata=rule_metadata_from_shipped_registry,
            session=in_memory_session,
        )
    assert output_2 is not None

    inv_2_kinds = [entry.event_kind for entry in output_2.audit_log_entries]
    assert "regime_transition" in inv_2_kinds
    assert "regime_skip_emergency" in inv_2_kinds
    transition_entries = [
        entry for entry in output_2.audit_log_entries if entry.event_kind == "regime_transition"
    ]
    assert len(transition_entries) == 1
    assert transition_entries[0].payload["direction"] == "tightening"
    assert transition_entries[0].payload["prior_regime"] == Regime.low_vol.value
    assert transition_entries[0].payload["new_regime"] == Regime.crisis.value
