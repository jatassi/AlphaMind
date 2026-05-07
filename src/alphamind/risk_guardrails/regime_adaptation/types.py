"""Typed value objects for the regime-adaptation feature (story 02).

This module is the single source of truth for the records every downstream
regime-adaptation story consumes — the persisted ``RegimeAdaptationState``,
the orchestrator output ``RegimeAdaptationOutput``, the per-breach
``RegimeTransitionBreach``, the event-calendar shapes, the per-overlay
``OverlayActivationDecision``, the orchestrator audit entry, and a handful of
adapter / passthrough records the surrounding stories will read.

Each record is a stdlib ``dataclass(frozen=True, slots=True)``. Pydantic
records from ``portfolio_state`` and ``config`` are referenced via type
annotations rather than re-wrapped here.

Reading:
``docs/implementation/06-risk-guardrails/regime-adaptation/02-package-skeleton-and-types.md``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# Volatility-regime enums (canonical homes — re-exported from
# ``portfolio_state.records.capital`` for backward compatibility).
#
# These two enums are defined *before* every other import in this module so
# that downstream re-exporters (``portfolio_state.records.capital`` and
# ``alphamind.persistence.models``, both transitively pulled in by the
# config / distillation imports below) can resolve them without hitting a
# partial-load ``ImportError``.
# ---------------------------------------------------------------------------


class RegimeLabel(StrEnum):
    """Volatility regime classification."""

    LOW_VOL = "LOW_VOL"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    CRISIS = "CRISIS"


class RegimeTransitionState(StrEnum):
    """Whether the system is stable or transitioning between volatility regimes."""

    STABLE = "STABLE"
    TIGHTENING = "TIGHTENING"
    LOOSENING = "LOOSENING"


# The imports below sit *after* the enum definitions to break the import
# cycle: ``alphamind.persistence.models`` and
# ``alphamind.portfolio_state.records.capital`` both need
# ``RegimeLabel``/``RegimeTransitionState`` from this module, and several
# config modules transitively load through them. The trailing E402
# suppressions on each import are therefore deliberate.
from alphamind.config.models.overlays import EventType, Overlay  # noqa: E402
from alphamind.config.models.regimes import Regime  # noqa: E402
from alphamind.config.resolver import LoadedConfig  # noqa: E402
from alphamind.distillation.calibration import CalibrationState  # noqa: E402
from alphamind.portfolio_state.records.positions import PositionRecord  # noqa: E402

if TYPE_CHECKING:
    # ``DistillationRegimeLabel`` (from ``distillation.regime``) is used only
    # as an annotation; importing it at runtime would pull in the
    # distillation baselines module, which transitively loads
    # ``persistence.models`` — a back-reference into this module via
    # ``RegimeTransitionState``.
    from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel

    # ``ActiveRiskParameterSet`` and ``RiskBudgetConsumption`` consume the
    # enums defined above, so importing them eagerly would cycle through
    # ``portfolio_state.records.capital``. They appear only as field-type
    # annotations on frozen dataclasses, so ``from __future__ import
    # annotations`` defers evaluation to string form — runtime imports are
    # not required.
    from alphamind.portfolio_state.records.capital import (
        ActiveRiskParameterSet,
        RiskBudgetConsumption,
    )


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

LOOSENING_INVOCATIONS: int = 3
"""Number of invocations across which a loosening transition's per-rule limits
linearly interpolate from origin to destination.

Mirrors ``regimes/*.yaml``'s ``transition.loosen_on_exit:
linear_over_invocations_3`` directive. Single source of truth for the state
machine (story 04b) and the interpolation primitive (story 04c)."""


# ---------------------------------------------------------------------------
# Tolerance for breach overage identity check
# ---------------------------------------------------------------------------

_BREACH_OVERAGE_ABS_TOL: float = 1e-9
"""Absolute tolerance applied to the ``RegimeTransitionBreach`` overage
identity check (``overage == current_value - new_limit_value``)."""


# ---------------------------------------------------------------------------
# RegimeAdaptationState — persisted snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeAdaptationState:
    """One invocation's regime-adaptation state, persisted forward-only.

    The persistence story (04a) reads back exactly this shape; the
    interpolation primitive (04c) reconstructs the linear interpolation
    state from a single persisted row using
    ``transition_started_invocation_id`` plus
    ``transition_origin_regime``.
    """

    as_of: str
    invocation_id: str
    active_regime: Regime
    prior_regime: Regime | None
    transition_state: RegimeTransitionState
    transition_invocations_remaining: int
    transition_started_invocation_id: str | None
    transition_origin_regime: Regime | None
    active_overlays: tuple[Overlay, ...]
    distillation_regime_label: str
    distillation_vix_level: float
    regime_skip_emergency: bool

    def __post_init__(self) -> None:
        if (
            self.transition_state == RegimeTransitionState.STABLE
            and self.transition_invocations_remaining != 0
        ):
            msg = (
                "transition_invocations_remaining must be 0 when transition_state "
                f"is STABLE; got transition_invocations_remaining="
                f"{self.transition_invocations_remaining}"
            )
            raise ValueError(msg)
        if self.transition_state == RegimeTransitionState.LOOSENING:
            if self.transition_started_invocation_id is None:
                msg = (
                    "transition_started_invocation_id is required when "
                    "transition_state is LOOSENING"
                )
                raise ValueError(msg)
            if self.transition_origin_regime is None:
                msg = "transition_origin_regime is required when transition_state is LOOSENING"
                raise ValueError(msg)


# ---------------------------------------------------------------------------
# RegimeTransitionBreach — per-position or per-rule breach record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeTransitionBreach:
    """A rule now exceeding a tightened regime limit.

    Two flavors, distinguished by ``position_id``:

    1. Per-position rule breach (``position_id`` is set): a rule whose limit
       applies per-position has at least one position above the tightened
       limit. One record per breaching position. Examples:
       ``position_max_size_pct``, ``single_short_max_pct``.
    2. Aggregate rule breach (``position_id`` is None): a rule whose limit
       applies to a portfolio-level aggregate has the aggregate above the
       tightened limit. One record per breaching rule. Examples:
       ``sector_concentration_*``, ``net_long_pct``, ``options_delta_pct``.

    Consumed by state-delivery's strategist guardrail state header (the
    ``Regime-transition breaches`` block) and the PM guardrail state header.
    """

    position_id: str | None
    rule_id: str
    rule_label: str
    current_value: float
    new_limit_value: float
    overage: float
    unit: str

    def __post_init__(self) -> None:
        if self.overage <= 0:
            msg = (
                f"RegimeTransitionBreach.overage must be strictly positive for rule_id="
                f"{self.rule_id!r}; got overage={self.overage}"
            )
            raise ValueError(msg)
        expected_overage = self.current_value - self.new_limit_value
        if not math.isclose(self.overage, expected_overage, abs_tol=_BREACH_OVERAGE_ABS_TOL):
            msg = (
                f"RegimeTransitionBreach.overage ({self.overage}) must equal "
                f"current_value - new_limit_value ({expected_overage}) for "
                f"rule_id={self.rule_id!r}"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Event calendar shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventCalendarEntry:
    """One scheduled high-impact catalyst.

    Earnings clusters are represented as a single ``earnings`` entry per
    cluster (the operator decides cluster boundaries when curating the
    calendar).
    """

    event_type: EventType
    event_timestamp_utc: datetime
    label: str

    def __post_init__(self) -> None:
        if self.event_timestamp_utc.tzinfo is None or self.event_timestamp_utc.utcoffset() is None:
            msg = "event_timestamp_utc must be timezone-aware"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class EventCalendar:
    """The full operator-curated calendar of scheduled catalysts."""

    entries: tuple[EventCalendarEntry, ...]


# ---------------------------------------------------------------------------
# OverlayActivationDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverlayActivationDecision:
    """One overlay's per-invocation activation result.

    ``pre_event_block_new_positions`` is overlay-specific: it is ``True``
    only on the final invocation before a pre-event activation, and
    ``False`` for the stress overlay regardless. Co-located here so the
    parameter-set assembler does not need to introspect the overlay enum to
    decide which auxiliary fields to surface.
    """

    overlay: Overlay
    is_active: bool
    rationale: str
    pre_event_block_new_positions: bool

    def __post_init__(self) -> None:
        if self.pre_event_block_new_positions:
            if self.overlay != Overlay.pre_event:
                msg = (
                    "pre_event_block_new_positions=True is only valid for "
                    f"Overlay.pre_event; got overlay={self.overlay!r}"
                )
                raise ValueError(msg)
            if not self.is_active:
                msg = (
                    "pre_event_block_new_positions=True requires is_active=True; "
                    "cannot block on an inactive overlay"
                )
                raise ValueError(msg)


# ---------------------------------------------------------------------------
# RegimeAdaptationAuditEntry
# ---------------------------------------------------------------------------


class RegimeAdaptationAuditEventKind(StrEnum):
    """Closed vocabulary of audit-log event kinds the orchestrator emits.

    The orchestrator is the only producer; the activity-log table CHECK
    constraint will mirror these member values when the table ships.
    """

    regime_transition = "regime_transition"
    overlay_activated = "overlay_activated"
    overlay_deactivated = "overlay_deactivated"
    regime_skip_emergency = "regime_skip_emergency"
    stale_event_calendar = "stale_event_calendar"


@dataclass(frozen=True, slots=True)
class RegimeAdaptationAuditEntry:
    """An activity-log entry the orchestrator emits for review-surface visibility.

    ``event_kind`` is a :class:`RegimeAdaptationAuditEventKind` member.
    Activity-log persistence is not in scope for this work tree — the
    orchestrator returns these entries as typed records and the caller
    decides when persistence wires up.
    """

    event_kind: RegimeAdaptationAuditEventKind
    payload: Mapping[str, object]


# ---------------------------------------------------------------------------
# RegimeAdaptationOutput — orchestrator return value
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeAdaptationOutput:
    """The orchestrator's per-invocation output bundle.

    The orchestrator does not perform persistence side-effects itself; it
    returns the new state and the audit log entries for the pipeline runtime
    to commit alongside the rest of the invocation's writes.

    ``overlay_activation_decisions`` surfaces the per-overlay activator
    outputs (one per registered overlay) so downstream consumers can read
    overlay-specific auxiliary fields such as
    ``pre_event_block_new_positions`` without re-running the activators.
    Ordered alphabetically by overlay value to mirror
    ``runtime_dimensions_active_overlays``.
    """

    runtime_dimensions_active_regime: Regime
    runtime_dimensions_active_overlays: tuple[Overlay, ...]
    overlay_activation_decisions: tuple[OverlayActivationDecision, ...]
    effective_limits: Mapping[str, float]
    active_risk_parameter_set: ActiveRiskParameterSet
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...]
    regime_skip_emergency: bool
    new_persisted_state: RegimeAdaptationState
    audit_log_entries: tuple[RegimeAdaptationAuditEntry, ...]


# ---------------------------------------------------------------------------
# VixBoundaryThresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VixBoundaryThresholds:
    """The three VIX-band boundaries that classify the current VIX into a
    guardrail regime.

    Sourced from ``RegimeClassification`` in distillation config; consumed
    by ``regime_mapping.map_distillation_to_guardrail_regime`` (story 03)
    and the orchestrator (story 09).
    """

    low_vol_vix_max: float
    normal_vix_max: float
    elevated_vix_max: float

    def __post_init__(self) -> None:
        if self.low_vol_vix_max <= 0 or self.normal_vix_max <= 0 or self.elevated_vix_max <= 0:
            msg = (
                "VixBoundaryThresholds requires every value strictly positive; got "
                f"low_vol_vix_max={self.low_vol_vix_max}, "
                f"normal_vix_max={self.normal_vix_max}, "
                f"elevated_vix_max={self.elevated_vix_max}"
            )
            raise ValueError(msg)
        if not (self.low_vol_vix_max < self.normal_vix_max < self.elevated_vix_max):
            msg = (
                "VixBoundaryThresholds requires "
                "low_vol_vix_max < normal_vix_max < elevated_vix_max; got "
                f"low_vol_vix_max={self.low_vol_vix_max}, "
                f"normal_vix_max={self.normal_vix_max}, "
                f"elevated_vix_max={self.elevated_vix_max}"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# NextTransitionDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NextTransitionDecision:
    """The transition-state-machine output the orchestrator persists.

    Produced by ``transition_machine.compute_next_transition`` (story 04b);
    consumed by ``parameter_set.assemble_active_risk_parameter_set`` (story
    08) and the orchestrator (story 09).

    The orchestrator (story 09) constructs the full ``RegimeAdaptationState``
    by combining this decision with the auxiliary passthrough fields
    (``distillation_regime_label``, ``distillation_vix_level``,
    ``regime_skip_emergency``, ``active_overlays``, ``as_of``).
    """

    active_regime: Regime
    prior_regime: Regime | None
    transition_state: RegimeTransitionState
    transition_invocations_remaining: int
    transition_started_invocation_id: str | None
    transition_origin_regime: Regime | None


# ---------------------------------------------------------------------------
# CompositeAlertState
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompositeAlertState:
    """Funding-stress and market-liquidity composite alert flags read from
    the distillation layer's persisted state.

    Populated by ``stress_activator.fetch_composite_alert_state(session)``
    (story 06b's DB-read helper). Consumed by ``evaluate_stress_overlay``
    (story 06b) and the orchestrator (story 09).
    """

    funding_stress_alert_active: bool
    funding_stress_calibration_state: CalibrationState
    market_liquidity_alert_active: bool
    market_liquidity_calibration_state: CalibrationState
    funding_stress_as_of: str | None
    market_liquidity_as_of: str | None


# ---------------------------------------------------------------------------
# RuleMetadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleMetadata:
    """Minimal per-rule metadata for the breach detector's render-context emission.

    Constructed from ``RuleRegistry`` (rules-and-limits work tree) by the
    orchestrator (story 09) and passed through to the breach detector
    (story 07). Decouples the breach detector from the full ``RuleEntry``
    shape so the registry's evolution does not ripple through.
    """

    rule_id: str
    label: str
    unit: str


# ---------------------------------------------------------------------------
# StaleCalendarReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleCalendarReport:
    """Output of the event-calendar staleness check.

    Produced by ``event_calendar.warn_on_stale_calendar`` (story 05);
    consumed by the orchestrator (story 09) which surfaces the report via
    the ``stale_event_calendar`` audit-log entry. An empty calendar is
    ``is_stale=False, days_until_latest=None``.
    """

    is_stale: bool
    latest_event_timestamp_utc: datetime | None
    days_until_latest: float | None


# ---------------------------------------------------------------------------
# RegimeAdaptationInputs — orchestrator input bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeAdaptationInputs:
    """All non-clock, non-DB inputs the orchestrator (story 09) consumes.

    Bundled following the ``compose_config(inputs: LoadedConfig, runtime:
    RuntimeDimensions)`` precedent so the orchestrator's call surface stays
    narrow. The pipeline-runtime caller constructs this record from the
    upstream distillation classifier output, the portfolio-state assembler
    output, the operator-loaded config, the event-calendar loader, and the
    stress-overlay composite-alert reader.

    The four ``distillation_*`` fields plus ``vix_thresholds`` are the
    distillation layer's contribution; the next three fields
    (``held_positions``, ``risk_budget``, ``prior_parameter_set``) come from
    the portfolio-state layer; ``event_calendar`` and ``composite_alert_state``
    come from the regime-adaptation feature's own loaders; ``loaded_config``
    is the resolver's input bundle; ``rule_metadata`` is constructed by the
    caller from ``RuleRegistry`` (rules-and-limits feature).
    """

    distillation_regime_label: DistillationRegimeLabel
    distillation_vix_level: float
    distillation_regime_skip_emergency: bool
    vix_thresholds: VixBoundaryThresholds
    held_positions: tuple[PositionRecord, ...]
    risk_budget: RiskBudgetConsumption
    prior_parameter_set: ActiveRiskParameterSet | None
    event_calendar: EventCalendar
    composite_alert_state: CompositeAlertState
    loaded_config: LoadedConfig
    rule_metadata: Mapping[str, RuleMetadata]


# ---------------------------------------------------------------------------
# overlays_to_strings — single source of truth for the Overlay→sorted-string
# tuple conversion used by both the parameter-set assembler (story 08) and
# the orchestrator (story 09).
# ---------------------------------------------------------------------------


def overlays_to_strings(overlays: tuple[Overlay, ...]) -> tuple[str, ...]:
    """Convert an ``Overlay``-enum tuple to the alphabetically-sorted
    string-tuple shape required by
    ``portfolio_state.records.capital.ActiveRiskParameterSet.active_overlays``.

    Both consumers (the parameter-set assembler and the orchestrator) call
    this helper rather than open-coding the lambda; downstream tier-override
    appenders (e.g., ``cumulative_drawdown_tier_{N}``) preserve the sort by
    re-sorting after appending.
    """
    return tuple(sorted(overlay.value for overlay in overlays))
