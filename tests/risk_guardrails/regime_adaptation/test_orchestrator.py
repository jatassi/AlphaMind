"""Tests for ``resolve_regime_adaptation`` — story 09.

The orchestrator composes every prior story's primitive into a single
``RegimeAdaptationOutput``. Its only DB seam is ``select_most_recent_state``;
all other inputs are passed in. Tests exercise:

- The bootstrap path (no prior state → STABLE).
- Stable continuation, tightening, loosening transitions and overrides.
- Overlay activation / deactivation flows.
- Audit-log emission predicates for each event kind.
- Pass-through of the regime-skip emergency flag.
- Stale event-calendar surfacing.
- Determinism.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.config.loaders import (
    load_modes,
    load_overlays,
    load_profiles,
    load_regimes,
    load_run_types,
)
from alphamind.config.models.agents import AgentsConfig
from alphamind.config.models.assets import AssetsConfig
from alphamind.config.models.digest import DigestConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.guardrails import GuardrailsConfig
from alphamind.config.models.llm_failure import LLMFailureConfig
from alphamind.config.models.main import MainConfig
from alphamind.config.models.overlays import (
    EventType,
    FinalInvocationBeforeEvent,
    Overlay,
    PreEventActivation,
    PreEventOverlay,
)
from alphamind.config.models.regimes import Regime
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.config.resolver import LoadedConfig
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    RegimeLabel,
    RegimeTransitionState,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskZone,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.risk_guardrails.regime_adaptation import (
    CompositeAlertState,
    EventCalendar,
    EventCalendarEntry,
    RegimeAdaptationAuditEventKind,
    RegimeAdaptationInputs,
    RegimeAdaptationOutput,
    RegimeAdaptationState,
    RuleMetadata,
    VixBoundaryThresholds,
    insert_state,
    resolve_regime_adaptation,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read_yaml(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


def _multiplier_snapshot(payload: Mapping[str, object], key: str) -> dict[str, float]:
    """Cast a multiplier-snapshot field on an audit payload to ``dict[str, float]``.

    The orchestrator stores per-rule multiplier maps under ``Mapping[str, object]``
    payloads; this helper hides the cast at the test boundary so individual
    assertions stay readable.
    """
    snapshot = payload[key]
    return dict(cast(Mapping[str, float], snapshot))


# ---------------------------------------------------------------------------
# Constant fixtures used across tests
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
_INGESTED_AT = "2026-04-29T13:30:01Z"


# ---------------------------------------------------------------------------
# DB session fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Builders for each input the orchestrator consumes.
# ---------------------------------------------------------------------------


# Tests load the production YAMLs so the orchestrator sees real profile/regime
# values; this also keeps the test surface aligned with operator-facing knobs.
_LOADED_CONFIG_CACHE: LoadedConfig | None = None


def _loaded_config() -> LoadedConfig:
    """Build a real ``LoadedConfig`` from the repo's ``config/`` tree.

    Cached once per session so the YAML I/O (file parsing) does not dominate
    test runtime. The orchestrator does not mutate the loaded config; sharing
    the instance across tests is safe.
    """
    global _LOADED_CONFIG_CACHE
    if _LOADED_CONFIG_CACHE is None:
        _LOADED_CONFIG_CACHE = LoadedConfig(
            main=MainConfig.model_validate(_read_yaml("main.yaml")),
            scheduler=SchedulerConfig.model_validate(_read_yaml("scheduler.yaml")),
            venue=VenueConfig.model_validate(_read_yaml("venue.yaml")),
            execution=ExecutionConfig.model_validate(_read_yaml("execution.yaml")),
            guardrails=GuardrailsConfig.model_validate(_read_yaml("guardrails.yaml")),
            llm_failure=LLMFailureConfig.model_validate(_read_yaml("llm_failure.yaml")),
            digest=DigestConfig.model_validate(_read_yaml("digest.yaml")),
            assets=AssetsConfig.model_validate(_read_yaml("assets.yaml")),
            agents=AgentsConfig.model_validate(_read_yaml("agents.yaml")),
            profiles=load_profiles(CONFIG_DIR),
            regimes=load_regimes(CONFIG_DIR),
            modes=load_modes(CONFIG_DIR),
            overlays=load_overlays(CONFIG_DIR),
            run_types=load_run_types(CONFIG_DIR),
        )
    return _LOADED_CONFIG_CACHE


def _profile_rule_values() -> dict[str, float]:
    """Return the active profile's per-rule base values from the loaded config."""
    config = _loaded_config()
    profile = config.profiles[config.main.active_profile]
    return dict(profile.rule_values)


def _build_rule_metadata() -> dict[str, RuleMetadata]:
    """Build rule metadata covering every rule the active profile declares."""
    return {
        rule_id: RuleMetadata(rule_id=rule_id, label=rule_id.replace("_", " "), unit="pct")
        for rule_id in _profile_rule_values()
    }


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


def _stress_alert_state() -> CompositeAlertState:
    return CompositeAlertState(
        funding_stress_alert_active=True,
        funding_stress_calibration_state=CalibrationState.CALIBRATED,
        funding_stress_as_of="2026-04-29T13:00:00Z",
        market_liquidity_alert_active=False,
        market_liquidity_calibration_state=CalibrationState.CALIBRATED,
        market_liquidity_as_of=None,
    )


def _empty_calendar() -> EventCalendar:
    return EventCalendar(entries=())


def _empty_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _zero_positions() -> tuple[PositionRecord, ...]:
    return ()


def _equity_position(*, position_id: str, position_weight_pct: float) -> PositionRecord:
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        instrument_type=InstrumentType.EQUITY,
        equity_details=EquityPositionDetails(
            ticker="AAPL",
            share_count=10.0,
            average_cost_basis_per_share=100.0,
            borrow_rate_pct=None,
            locate_status=None,
            margin_held_usd=None,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=100.0,
                fill_quantity=10.0,
                slippage=0.01,
                fees=0.5,
            ),
        ),
        realized_pnl_to_date_usd=None,
        current_market_value_usd=1000.0,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=0.0,
        notional_exposure_usd=1000.0,
        delta_adjusted_exposure_usd=1000.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _build_inputs(
    *,
    distillation_regime_label: DistillationRegimeLabel = (
        DistillationRegimeLabel.LOW_VOL_COMPRESSION
    ),
    distillation_vix_level: float = 10.0,
    distillation_regime_skip_emergency: bool = False,
    held_positions: tuple[PositionRecord, ...] | None = None,
    risk_budget: RiskBudgetConsumption | None = None,
    prior_parameter_set: ActiveRiskParameterSet | None = None,
    event_calendar: EventCalendar | None = None,
    composite_alert_state: CompositeAlertState | None = None,
    loaded_config: LoadedConfig | None = None,
    rule_metadata: dict[str, RuleMetadata] | None = None,
    vix_thresholds: VixBoundaryThresholds | None = None,
) -> RegimeAdaptationInputs:
    return RegimeAdaptationInputs(
        distillation_regime_label=distillation_regime_label,
        distillation_vix_level=distillation_vix_level,
        distillation_regime_skip_emergency=distillation_regime_skip_emergency,
        vix_thresholds=vix_thresholds if vix_thresholds is not None else _vix_thresholds(),
        held_positions=held_positions if held_positions is not None else _zero_positions(),
        risk_budget=risk_budget if risk_budget is not None else _empty_risk_budget(),
        prior_parameter_set=prior_parameter_set,
        event_calendar=event_calendar if event_calendar is not None else _empty_calendar(),
        composite_alert_state=(
            composite_alert_state if composite_alert_state is not None else _quiet_alert_state()
        ),
        loaded_config=loaded_config if loaded_config is not None else _loaded_config(),
        rule_metadata=rule_metadata if rule_metadata is not None else _build_rule_metadata(),
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    """No prior persisted state, low-vol distillation, no overlays."""

    def test_returns_stable_low_vol_with_no_audit_entries(self, session: Session) -> None:
        inputs = _build_inputs()

        output = resolve_regime_adaptation(
            invocation_id="INV-1",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert isinstance(output, RegimeAdaptationOutput)
        assert output.runtime_dimensions_active_regime == Regime.low_vol
        assert output.runtime_dimensions_active_overlays == ()
        assert output.regime_transition_breaches == ()
        assert output.regime_skip_emergency is False
        assert output.audit_log_entries == ()
        assert output.new_persisted_state.transition_state == RegimeTransitionState.STABLE
        assert output.new_persisted_state.prior_regime is None
        assert output.new_persisted_state.active_regime == Regime.low_vol
        assert output.active_risk_parameter_set.regime_label == RegimeLabel.LOW_VOL


# ---------------------------------------------------------------------------
# Stable continuation
# ---------------------------------------------------------------------------


def _persist_prior(
    session: Session,
    *,
    active_regime: Regime,
    transition_state: RegimeTransitionState = RegimeTransitionState.STABLE,
    transition_invocations_remaining: int = 0,
    transition_started_invocation_id: str | None = None,
    transition_origin_regime: Regime | None = None,
    active_overlays: tuple[Overlay, ...] = (),
    distillation_regime_label: str = "vol_expansion",
    distillation_vix_level: float = 18.0,
    regime_skip_emergency: bool = False,
    invocation_id: str = "INV-PRIOR",
    as_of: str = "2026-04-29T12:00:00Z",
) -> RegimeAdaptationState:
    state = RegimeAdaptationState(
        as_of=as_of,
        invocation_id=invocation_id,
        active_regime=active_regime,
        prior_regime=None,
        transition_state=transition_state,
        transition_invocations_remaining=transition_invocations_remaining,
        transition_started_invocation_id=transition_started_invocation_id,
        transition_origin_regime=transition_origin_regime,
        active_overlays=active_overlays,
        distillation_regime_label=distillation_regime_label,
        distillation_vix_level=distillation_vix_level,
        regime_skip_emergency=regime_skip_emergency,
    )
    insert_state(session, state, ingested_at=_INGESTED_AT)
    return state


class TestStableContinuation:
    """Prior STABLE in normal; same regime → STABLE, no transition audit."""

    def test_no_regime_transition_event_emitted(self, session: Session) -> None:
        _persist_prior(session, active_regime=Regime.normal)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-2",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert output.new_persisted_state.transition_state == RegimeTransitionState.STABLE
        assert output.new_persisted_state.active_regime == Regime.normal
        kinds = [entry.event_kind for entry in output.audit_log_entries]
        assert "regime_transition" not in kinds


# ---------------------------------------------------------------------------
# Tightening transition
# ---------------------------------------------------------------------------


class TestTighteningTransition:
    """Prior STABLE in normal; new CRISIS_SPIKE → TIGHTENING in crisis."""

    def test_emits_regime_transition_audit_with_tightening_direction(
        self, session: Session
    ) -> None:
        _persist_prior(session, active_regime=Regime.normal)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-3",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert output.new_persisted_state.transition_state == RegimeTransitionState.TIGHTENING
        assert output.new_persisted_state.active_regime == Regime.crisis
        transition_entries = [
            entry for entry in output.audit_log_entries if entry.event_kind == "regime_transition"
        ]
        assert len(transition_entries) == 1
        payload = transition_entries[0].payload
        assert payload["direction"] == "tightening"
        assert payload["prior_regime"] == "normal"
        assert payload["new_regime"] == "crisis"

    def test_payload_carries_static_and_applied_multipliers(self, session: Session) -> None:
        """Tightening from STABLE: applied_prior == static_prior; applied_new == static_new."""
        _persist_prior(session, active_regime=Regime.normal)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-TIGHT-MULTS",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        transition_entries = [
            entry for entry in output.audit_log_entries if entry.event_kind == "regime_transition"
        ]
        assert len(transition_entries) == 1
        payload = transition_entries[0].payload

        config = _loaded_config()
        static_prior = dict(config.regimes[Regime.normal].multipliers)
        static_new = dict(config.regimes[Regime.crisis].multipliers)

        assert _multiplier_snapshot(payload, "static_prior_multipliers_snapshot") == static_prior
        assert _multiplier_snapshot(payload, "static_new_multipliers_snapshot") == static_new
        # Tightening from STABLE: applied prior equals static prior.
        assert _multiplier_snapshot(payload, "applied_prior_multipliers_snapshot") == static_prior
        # Tightening applies the destination regime's multipliers immediately.
        assert _multiplier_snapshot(payload, "applied_new_multipliers_snapshot") == static_new


class TestTighteningWithBreaches:
    """Tightening transition with held positions over the new tighter limit."""

    def test_emits_regime_transition_breach_records(self, session: Session) -> None:
        _persist_prior(session, active_regime=Regime.normal)
        # crisis position_max_size_pct: 5.0 * 0.40 = 2.0; position is at 4.0
        breaching = _equity_position(position_id="POS-1", position_weight_pct=4.0)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
            held_positions=(breaching,),
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-4",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert len(output.regime_transition_breaches) >= 1
        breach = output.regime_transition_breaches[0]
        assert breach.rule_id == "position_max_size_pct"
        assert breach.position_id == "POS-1"


# ---------------------------------------------------------------------------
# Loosening transition
# ---------------------------------------------------------------------------


class TestLooseningFirstInvocation:
    """Prior STABLE in crisis; new VOL_EXPANSION at vix=18 → LOOSENING."""

    def test_remaining_starts_at_three_with_origin_crisis(self, session: Session) -> None:
        _persist_prior(session, active_regime=Regime.crisis)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-5",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        new_state = output.new_persisted_state
        assert new_state.transition_state == RegimeTransitionState.LOOSENING
        assert new_state.transition_invocations_remaining == 3
        assert new_state.transition_origin_regime == Regime.crisis
        assert new_state.active_regime == Regime.normal

        transition_entries = [
            entry for entry in output.audit_log_entries if entry.event_kind == "regime_transition"
        ]
        assert len(transition_entries) == 1
        assert transition_entries[0].payload["direction"] == "loosening"


class TestLooseningCountdown:
    """Prior LOOSENING with remaining=3, same VOL_EXPANSION reading → remaining=2."""

    def test_decrements_remaining_without_new_transition_audit(self, session: Session) -> None:
        _persist_prior(
            session,
            active_regime=Regime.normal,
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=3,
            transition_started_invocation_id="INV-PRIOR",
            transition_origin_regime=Regime.crisis,
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-6",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        new_state = output.new_persisted_state
        assert new_state.transition_state == RegimeTransitionState.LOOSENING
        assert new_state.transition_invocations_remaining == 2
        kinds = [entry.event_kind for entry in output.audit_log_entries]
        assert "regime_transition" not in kinds


class TestLooseningCompletion:
    """Prior LOOSENING with remaining=1 → STABLE arrival, no transition audit."""

    def test_returns_stable_with_no_audit_entry(self, session: Session) -> None:
        _persist_prior(
            session,
            active_regime=Regime.normal,
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=1,
            transition_started_invocation_id="INV-PRIOR",
            transition_origin_regime=Regime.crisis,
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-7",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        new_state = output.new_persisted_state
        assert new_state.transition_state == RegimeTransitionState.STABLE
        assert new_state.transition_invocations_remaining == 0
        kinds = [entry.event_kind for entry in output.audit_log_entries]
        assert "regime_transition" not in kinds


class TestTighteningOverridesLoosening:
    """Prior LOOSENING in normal from elevated; new CRISIS_SPIKE → TIGHTENING in crisis."""

    def test_emits_tightening_audit_and_resets_state(self, session: Session) -> None:
        _persist_prior(
            session,
            active_regime=Regime.normal,
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=2,
            transition_started_invocation_id="INV-OLD",
            transition_origin_regime=Regime.elevated,
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-8",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert output.new_persisted_state.transition_state == RegimeTransitionState.TIGHTENING
        assert output.new_persisted_state.active_regime == Regime.crisis
        transition_entries = [
            entry for entry in output.audit_log_entries if entry.event_kind == "regime_transition"
        ]
        assert len(transition_entries) == 1
        assert transition_entries[0].payload["direction"] == "tightening"

    def test_applied_prior_reflects_loosening_interpolation(self, session: Session) -> None:
        """Prior LOOSENING: applied_prior_multipliers_snapshot is the interpolated map.

        With prior state ``LOOSENING(remaining=2, origin=elevated, active=normal)``,
        the prior invocation ran at the interpolation between elevated and normal at
        invocation 2 of 3 — i.e. fraction = (3 - 2 + 1) / 3 = 2/3 along the path
        from elevated → normal.
        """
        from alphamind.risk_guardrails.regime_adaptation.interpolation import (
            interpolate_loosening_multipliers,
        )

        _persist_prior(
            session,
            active_regime=Regime.normal,
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=2,
            transition_started_invocation_id="INV-OLD",
            transition_origin_regime=Regime.elevated,
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-LOOSE-TIGHT",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        transition_entries = [
            entry for entry in output.audit_log_entries if entry.event_kind == "regime_transition"
        ]
        assert len(transition_entries) == 1
        payload = transition_entries[0].payload

        config = _loaded_config()
        static_prior = dict(config.regimes[Regime.normal].multipliers)
        static_new = dict(config.regimes[Regime.crisis].multipliers)
        expected_applied_prior = dict(
            interpolate_loosening_multipliers(
                origin_multipliers=config.regimes[Regime.elevated].multipliers,
                destination_multipliers=config.regimes[Regime.normal].multipliers,
                transition_invocations_remaining=2,
            )
        )

        assert _multiplier_snapshot(payload, "static_prior_multipliers_snapshot") == static_prior
        assert _multiplier_snapshot(payload, "static_new_multipliers_snapshot") == static_new
        assert (
            _multiplier_snapshot(payload, "applied_prior_multipliers_snapshot")
            == expected_applied_prior
        )
        # Tightening: applied new equals the static destination immediately.
        assert _multiplier_snapshot(payload, "applied_new_multipliers_snapshot") == static_new
        # Sanity: applied prior should differ from static prior because we were
        # mid-loosening from elevated → normal.
        assert _multiplier_snapshot(payload, "applied_prior_multipliers_snapshot") != static_prior


# ---------------------------------------------------------------------------
# Pre-event overlay activation
# ---------------------------------------------------------------------------


class TestPreEventOverlayActivates:
    """Calendar contains a pending FOMC inside the activation window."""

    def test_overlay_active_and_audit_emitted(self, session: Session) -> None:
        # _NOW is 13:30 UTC on a Tuesday in April. The market_hours_rolling cron
        # fires at 13:30 UTC (9:30 ET), 15:30 UTC (11:30 ET), 17:30 UTC, 19:30 UTC.
        # An FOMC at _NOW + 90 minutes (15:00 UTC) means there is exactly 1 firing
        # ahead of it (the 13:30 UTC firing, which happens at _NOW exactly so it's
        # NOT > now). With windows_before_event=2, 0 firings before the event
        # makes the current invocation the final pre-event firing.
        fomc_time = _NOW + timedelta(hours=1, minutes=30)
        calendar = EventCalendar(
            entries=(
                EventCalendarEntry(
                    event_type=EventType.fomc,
                    event_timestamp_utc=fomc_time,
                    label="FOMC announcement",
                ),
            )
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            event_calendar=calendar,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-9",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert Overlay.pre_event in output.runtime_dimensions_active_overlays
        # position_max_size_pct: base 5.0 * normal multiplier 1.0 * pre_event 0.80 = 4.0
        assert output.effective_limits["position_max_size_pct"] == pytest.approx(4.0)
        kinds = [entry.event_kind for entry in output.audit_log_entries]
        assert "overlay_activated" in kinds
        activations = [
            entry for entry in output.audit_log_entries if entry.event_kind == "overlay_activated"
        ]
        overlays_activated = [entry.payload["overlay"] for entry in activations]
        assert Overlay.pre_event in overlays_activated


class TestOverlayMultiplierKeyTypoRejected:
    """Overlay declaring a multiplier for a rule_id absent from the rule space raises."""

    def test_orchestrator_raises_value_error_naming_overlay_and_rule(
        self, session: Session
    ) -> None:
        from dataclasses import replace

        base_config = _loaded_config()
        typo_overlay = PreEventOverlay(
            activation=PreEventActivation(
                windows_before_event=2,
                events=[EventType.fomc],
            ),
            multipliers={"position_max_size_pcr": 0.80},  # typo'd: should be _pct
            final_invocation_before_event=FinalInvocationBeforeEvent(block_new_positions=True),
        )
        overlays_with_typo = dict(base_config.overlays)
        overlays_with_typo[Overlay.pre_event] = typo_overlay
        config_with_typo = replace(base_config, overlays=overlays_with_typo)

        fomc_time = _NOW + timedelta(hours=1, minutes=30)
        calendar = EventCalendar(
            entries=(
                EventCalendarEntry(
                    event_type=EventType.fomc,
                    event_timestamp_utc=fomc_time,
                    label="FOMC announcement",
                ),
            )
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            event_calendar=calendar,
            loaded_config=config_with_typo,
        )

        with pytest.raises(ValueError, match=r"pre_event.*position_max_size_pcr"):
            resolve_regime_adaptation(
                invocation_id="INV-TYPO",
                now_utc=_NOW,
                inputs=inputs,
                session=session,
            )


# ---------------------------------------------------------------------------
# Stress overlay activation
# ---------------------------------------------------------------------------


class TestStressOverlayActivates:
    """funding_stress alert active + CALIBRATED → stress overlay active."""

    def test_overlay_surfaces_in_output_with_audit_entry(self, session: Session) -> None:
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            composite_alert_state=_stress_alert_state(),
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-10",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert Overlay.stress in output.runtime_dimensions_active_overlays
        # sector_concentration_pct: base 25.0 * normal 1.0 * stress 0.85 = 21.25
        assert output.effective_limits["sector_concentration_pct"] == pytest.approx(21.25)
        # gross_exposure_pct: base 120.0 * 1.0 * 0.85 = 102.0
        assert output.effective_limits["gross_exposure_pct"] == pytest.approx(102.0)
        kinds = [entry.event_kind for entry in output.audit_log_entries]
        assert "overlay_activated" in kinds


# ---------------------------------------------------------------------------
# Both overlays active
# ---------------------------------------------------------------------------


class TestBothOverlaysActivate:
    """Pre-event firing + stress alert → both overlays active sorted alphabetically."""

    def test_overlays_sorted_and_both_audit_entries_emitted(self, session: Session) -> None:
        fomc_time = _NOW + timedelta(hours=1, minutes=30)
        calendar = EventCalendar(
            entries=(
                EventCalendarEntry(
                    event_type=EventType.fomc,
                    event_timestamp_utc=fomc_time,
                    label="FOMC announcement",
                ),
            )
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            event_calendar=calendar,
            composite_alert_state=_stress_alert_state(),
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-11",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        # Sorted by Overlay.value alphabetically: pre_event ("pre_event") < stress.
        assert output.runtime_dimensions_active_overlays == (Overlay.pre_event, Overlay.stress)
        activations = [
            entry for entry in output.audit_log_entries if entry.event_kind == "overlay_activated"
        ]
        assert len(activations) == 2


# ---------------------------------------------------------------------------
# Overlay deactivation
# ---------------------------------------------------------------------------


class TestOverlayDeactivates:
    """Prior had stress overlay; current has none → overlay_deactivated audit."""

    def test_emits_overlay_deactivated_for_stress(self, session: Session) -> None:
        _persist_prior(
            session,
            active_regime=Regime.normal,
            active_overlays=(Overlay.stress,),
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            composite_alert_state=_quiet_alert_state(),
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-12",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        deactivations = [
            entry for entry in output.audit_log_entries if entry.event_kind == "overlay_deactivated"
        ]
        assert len(deactivations) == 1
        assert deactivations[0].payload["overlay"] == Overlay.stress


# ---------------------------------------------------------------------------
# Regime-skip emergency
# ---------------------------------------------------------------------------


class TestRegimeSkipEmergencyPassthrough:
    """distillation_regime_skip_emergency=True → output flag True + audit entry."""

    def test_flag_passes_through_and_audit_emitted(self, session: Session) -> None:
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=45.0,
            distillation_regime_skip_emergency=True,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-13",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert output.regime_skip_emergency is True
        skip_entries = [
            entry
            for entry in output.audit_log_entries
            if entry.event_kind == "regime_skip_emergency"
        ]
        assert len(skip_entries) == 1
        payload = skip_entries[0].payload
        assert payload["distillation_regime_label"] == DistillationRegimeLabel.CRISIS_SPIKE.value
        assert payload["distillation_vix_level"] == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# Stale event calendar
# ---------------------------------------------------------------------------


class TestStaleEventCalendar:
    """Latest entry inside the 7-day staleness threshold → audit emitted."""

    def test_emits_stale_calendar_audit(self, session: Session) -> None:
        soon_event = _NOW + timedelta(days=5)
        calendar = EventCalendar(
            entries=(
                EventCalendarEntry(
                    event_type=EventType.cpi,
                    event_timestamp_utc=soon_event,
                    label="CPI release",
                ),
            )
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            event_calendar=calendar,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-14",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        stale_entries = [
            entry
            for entry in output.audit_log_entries
            if entry.event_kind == "stale_event_calendar"
        ]
        assert len(stale_entries) == 1


class TestNonStaleEventCalendar:
    """Latest entry beyond the staleness threshold → no stale audit."""

    def test_does_not_emit_stale_calendar_audit(self, session: Session) -> None:
        far_event = _NOW + timedelta(days=30)
        calendar = EventCalendar(
            entries=(
                EventCalendarEntry(
                    event_type=EventType.cpi,
                    event_timestamp_utc=far_event,
                    label="CPI release",
                ),
            )
        )
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            event_calendar=calendar,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-15",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        kinds = [entry.event_kind for entry in output.audit_log_entries]
        assert "stale_event_calendar" not in kinds


# ---------------------------------------------------------------------------
# Empty held positions / breaches
# ---------------------------------------------------------------------------


class TestNoHeldPositionsNoBreaches:
    """Tightening transition with empty held positions → no breach records."""

    def test_no_breaches_when_held_positions_empty(self, session: Session) -> None:
        _persist_prior(session, active_regime=Regime.normal)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-16",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert output.regime_transition_breaches == ()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Identical inputs produce identical outputs."""

    def test_repeated_calls_produce_equal_outputs(self, session: Session) -> None:
        inputs = _build_inputs()
        first = resolve_regime_adaptation(
            invocation_id="INV-DET",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )
        second = resolve_regime_adaptation(
            invocation_id="INV-DET",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )
        assert first == second


# ---------------------------------------------------------------------------
# Top-level package re-export
# ---------------------------------------------------------------------------


class TestPackageReExport:
    """``resolve_regime_adaptation`` is re-exported from the package ``__init__``."""

    def test_top_level_package_exposes_resolver(self) -> None:
        import alphamind.risk_guardrails.regime_adaptation as regime_adaptation

        assert regime_adaptation.resolve_regime_adaptation is resolve_regime_adaptation


# ---------------------------------------------------------------------------
# Aggregate (risk-budget) breach detection
# ---------------------------------------------------------------------------


def _budget_entry(rule_id: str, current_value: float) -> RiskBudgetEntry:
    limit = 100.0
    headroom = limit - current_value
    headroom_pct = max(0.0, min(100.0, 100.0 * headroom / limit))
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_id.replace("_", " "),
        current_value=current_value,
        limit_value=limit,
        headroom=headroom,
        headroom_pct_of_limit=headroom_pct,
        zone=RiskZone.NORMAL,
        unit="pct",
        cumulative_invocation_impact_value=0.0,
    )


class TestAggregateBreachOnTightening:
    """Tightening transition with risk-budget snapshot above the new aggregate limit.

    Per story 07's contract, the breach detector emits no breaches when
    ``held_positions`` is empty even if aggregate-rule budget entries are over
    the new tightened limit; the detector treats an empty portfolio as a no-op.
    The aggregate-rule path is therefore exercised here with at least one held
    position so the orchestrator's call into the detector reaches the aggregate
    scan branch.
    """

    def test_emits_aggregate_breach_record(self, session: Session) -> None:
        _persist_prior(session, active_regime=Regime.normal)
        # gross_exposure_pct base 120.0 * crisis 0.50 = 60.0; current value 80.0 breaches.
        budget = RiskBudgetConsumption(
            entries=(_budget_entry("gross_exposure_pct", current_value=80.0),)
        )
        anchor = _equity_position(position_id="ANCHOR", position_weight_pct=1.0)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
            held_positions=(anchor,),
            risk_budget=budget,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-AGG",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        gross_breaches = [
            breach
            for breach in output.regime_transition_breaches
            if breach.rule_id == "gross_exposure_pct"
        ]
        assert len(gross_breaches) == 1
        assert gross_breaches[0].position_id is None


# ---------------------------------------------------------------------------
# Empty composite-alert rows
# ---------------------------------------------------------------------------


class TestNoCompositeAlerts:
    """Quiet alert state → stress overlay inactive, no overlay_activated for stress."""

    def test_stress_overlay_does_not_activate(self, session: Session) -> None:
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.VOL_EXPANSION,
            distillation_vix_level=18.0,
            composite_alert_state=_quiet_alert_state(),
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-NS",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        assert Overlay.stress not in output.runtime_dimensions_active_overlays
        activations = [
            entry for entry in output.audit_log_entries if entry.event_kind == "overlay_activated"
        ]
        stress_activations = [
            entry for entry in activations if entry.payload["overlay"] == Overlay.stress
        ]
        assert stress_activations == []


# ---------------------------------------------------------------------------
# Audit event-kind enum
# ---------------------------------------------------------------------------


class TestAuditEventKindEnum:
    """``event_kind`` is a ``RegimeAdaptationAuditEventKind`` member, not a bare string."""

    def test_enum_members_cover_every_kind_emitted(self) -> None:
        kinds = {member.value for member in RegimeAdaptationAuditEventKind}
        assert kinds == {
            "regime_transition",
            "overlay_activated",
            "overlay_deactivated",
            "regime_skip_emergency",
            "stale_event_calendar",
        }

    def test_orchestrator_emits_enum_members(self, session: Session) -> None:
        _persist_prior(session, active_regime=Regime.normal)
        inputs = _build_inputs(
            distillation_regime_label=DistillationRegimeLabel.CRISIS_SPIKE,
            distillation_vix_level=40.0,
            distillation_regime_skip_emergency=True,
        )

        output = resolve_regime_adaptation(
            invocation_id="INV-ENUM",
            now_utc=_NOW,
            inputs=inputs,
            session=session,
        )

        for entry in output.audit_log_entries:
            assert isinstance(entry.event_kind, RegimeAdaptationAuditEventKind)
        kinds = {entry.event_kind for entry in output.audit_log_entries}
        assert RegimeAdaptationAuditEventKind.regime_transition in kinds
        assert RegimeAdaptationAuditEventKind.regime_skip_emergency in kinds
