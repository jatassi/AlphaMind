"""End-to-end Scenario C — regime-skip emergency passthrough (story 10).

A 2-invocation sequence that walks the orchestrator's
``regime_skip_emergency`` passthrough: a calm low-vol baseline followed
by a CRISIS_SPIKE marked as a regime-skip emergency by the upstream
distillation classifier. The orchestrator must:

- Forward the upstream flag verbatim to ``RegimeAdaptationOutput.regime_skip_emergency``.
- Emit one ``regime_skip_emergency`` audit entry on the emergency invocation.
- Emit a ``regime_transition`` audit entry as well, since the regime
  ladder index moved (low_vol → crisis is a tightening transition).

The emergency path does not interact with the transition state machine —
the orchestrator runs the same composition logic and additionally
surfaces the emergency flag. This test confirms the passthrough is wired
correctly and the two audit entries coexist.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from alphamind.config.models.regimes import Regime
from alphamind.config.resolver import LoadedConfig
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.risk_guardrails.regime_adaptation import (
    CompositeAlertState,
    EventCalendar,
    RegimeAdaptationInputs,
    RegimeAdaptationOutput,
    RuleMetadata,
    VixBoundaryThresholds,
    insert_state,
    resolve_regime_adaptation,
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


def _quiet_alert_state() -> CompositeAlertState:
    return CompositeAlertState(
        funding_stress_alert_active=False,
        funding_stress_calibration_state=CalibrationState.CALIBRATED,
        funding_stress_as_of=None,
        market_liquidity_alert_active=False,
        market_liquidity_calibration_state=CalibrationState.CALIBRATED,
        market_liquidity_as_of=None,
    )


def _build_inputs(
    *,
    distillation_regime_label: DistillationRegimeLabel,
    distillation_vix_level: float,
    distillation_regime_skip_emergency: bool,
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
) -> RegimeAdaptationInputs:
    return RegimeAdaptationInputs(
        distillation_regime_label=distillation_regime_label,
        distillation_vix_level=distillation_vix_level,
        distillation_regime_skip_emergency=distillation_regime_skip_emergency,
        vix_thresholds=_vix_thresholds(),
        held_positions=(),
        risk_budget=RiskBudgetConsumption(entries=()),
        prior_parameter_set=None,
        event_calendar=EventCalendar(entries=()),
        composite_alert_state=_quiet_alert_state(),
        loaded_config=loaded_config,
        rule_metadata=rule_metadata,
    )


def _drive_step(
    *,
    invocation_id: str,
    now_utc: datetime,
    distillation_regime_label: DistillationRegimeLabel,
    distillation_vix_level: float,
    distillation_regime_skip_emergency: bool,
    loaded_config: LoadedConfig,
    rule_metadata: Mapping[str, RuleMetadata],
    session: Session,
) -> RegimeAdaptationOutput:
    inputs = _build_inputs(
        distillation_regime_label=distillation_regime_label,
        distillation_vix_level=distillation_vix_level,
        distillation_regime_skip_emergency=distillation_regime_skip_emergency,
        loaded_config=loaded_config,
        rule_metadata=rule_metadata,
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


# ---------------------------------------------------------------------------
# Tests
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
    inv_1_now = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
    inv_2_now = inv_1_now + timedelta(hours=4)

    output_1 = _drive_step(
        invocation_id="INV-C-01",
        now_utc=inv_1_now,
        distillation_regime_label=DistillationRegimeLabel.LOW_VOL_COMPRESSION,
        distillation_vix_level=12.0,
        distillation_regime_skip_emergency=False,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
        session=in_memory_session,
    )
    assert output_1.regime_skip_emergency is False
    inv_1_kinds = {entry.event_kind for entry in output_1.audit_log_entries}
    assert "regime_skip_emergency" not in inv_1_kinds

    output_2 = _drive_step(
        invocation_id="INV-C-02",
        now_utc=inv_2_now,
        distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
        distillation_vix_level=50.0,
        distillation_regime_skip_emergency=True,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
        session=in_memory_session,
    )

    # Passthrough on the output record.
    assert output_2.regime_skip_emergency is True
    # Persisted state mirrors the input flag.
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
    inv_1_now = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
    inv_2_now = inv_1_now + timedelta(hours=4)

    _drive_step(
        invocation_id="INV-C-01",
        now_utc=inv_1_now,
        distillation_regime_label=DistillationRegimeLabel.LOW_VOL_COMPRESSION,
        distillation_vix_level=12.0,
        distillation_regime_skip_emergency=False,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
        session=in_memory_session,
    )
    output_2 = _drive_step(
        invocation_id="INV-C-02",
        now_utc=inv_2_now,
        distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
        distillation_vix_level=50.0,
        distillation_regime_skip_emergency=True,
        loaded_config=loaded_config_micro_normal,
        rule_metadata=rule_metadata_from_shipped_registry,
        session=in_memory_session,
    )

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
