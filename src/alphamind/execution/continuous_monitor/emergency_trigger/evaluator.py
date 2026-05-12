"""Emergency-invocation evaluator + supporting types (story 04b / ALP-439).

:class:`EmergencyTriggerEvaluator` is the ``on_emergency_input`` callback
the breach loop awaits once per tick. It maintains per-session rolling
windows of regime classifications and ``DrawdownSample`` observations,
composes the four trigger evaluators via
``evaluate_emergency_invocation``, enforces the cooldown via
:class:`CooldownTracker`, and writes one
``EMERGENCY_INVOCATION_REQUESTED`` activity-log entry per fire.

The evaluator is composed from the canonical primitives in
``alphamind.risk_guardrails.breach_behavior.emergency_triggers``: this
module does not re-implement regime-jump / multi-rule / velocity /
margin-call logic. It adapts the breach-loop's per-tick
``BreachLoopResult`` to the primitive composer's flat argument shape and
threads the activity-log writer.
"""

from __future__ import annotations

import logging
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Literal, Protocol

from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.trigger_ids import (
    TriggerIdGenerator,
)
from alphamind.execution.continuous_monitor.emergency_trigger.cooldown import (
    CooldownTracker,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EmergencyInvocationRequestedDetail,
    EventGroup,
    EventSource,
    EventType,
)
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    DrawdownSample,
    EmergencyTrigger,
    MarginCallEvent,
    RegimeLabel,
    evaluate_emergency_invocation,
)

log = logging.getLogger(__name__)

# Mirror of EmergencyInvocationRequestedDetail's ``trigger_type`` Literal —
# the receiver-side spec (ALP-447) accepts exactly these four values. Keep
# in sync if the detail class extends.
_TriggerType = Literal["regime_jump", "multi_rule_breach", "drawdown_velocity", "margin_call"]

# Map from ``EmergencyTrigger`` (the primitive's enum) → activity-log detail's
# trigger_type discriminator (``EmergencyInvocationRequestedDetail.trigger_type``).
# The two vocabularies differ in one place: the primitive uses
# ``daily_drawdown_velocity`` while the activity-log detail uses
# ``drawdown_velocity`` per the receiver-side schema (ALP-447).
_TRIGGER_TYPE_BY_PRIMITIVE: Mapping[EmergencyTrigger, _TriggerType] = {
    EmergencyTrigger.REGIME_JUMP: "regime_jump",
    EmergencyTrigger.MULTI_RULE_BREACH: "multi_rule_breach",
    EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY: "drawdown_velocity",
    EmergencyTrigger.MARGIN_CALL: "margin_call",
}


class MarginCallObserver(Protocol):
    """Probes broker state for an outstanding margin-call event.

    Returns the live :class:`MarginCallEvent` when one is outstanding, or
    ``None`` when no margin call is active. Production wires this against
    the broker adapter's account-state surface; tests substitute a fake.
    """

    async def __call__(self) -> MarginCallEvent | None: ...


class NoMarginCallObserver:
    """``MarginCallObserver`` that never returns a margin call.

    Used as the production default until the broker adapter exposes a
    margin-call observer. Operator deployments that need live margin-call
    sourcing can substitute a concrete observer via the wiring helper.
    """

    async def __call__(self) -> MarginCallEvent | None:
        return None


class ActivityLogWriter(Protocol):
    """Persists one :class:`ActivityLogEntry` per call.

    The production writer opens a fresh ``AsyncSession`` per call and
    commits; tests substitute an in-memory recorder. Mirrors the shape
    used by the greeks-refresh task (``make_activity_log_emitter``).
    """

    async def __call__(self, entry: ActivityLogEntry) -> None: ...


def _max_lookback_minutes(config: BreachBehaviorConfig) -> int:
    """Maximum lookback (minutes) the evaluator's rolling windows must retain.

    Regime classifications need the prior tick only (the primitive compares
    two adjacent labels). Drawdown velocity needs at most
    ``drawdown_velocity_window_minutes`` of history — anything older cannot
    cross within the window. Margin-call is point-in-time. We use the max
    of the two as the bound.
    """
    return max(2, config.drawdown_velocity_window_minutes)


def _project_risk_budget(
    rule_evaluations: tuple[RuleEvaluation, ...],
    *,
    breach_response_lookup: Mapping[str, BreachResponse],
) -> tuple[RiskBudgetConsumption, tuple[str, ...]]:
    """Project the breach loop's ``rule_evaluations`` into a synthetic
    ``RiskBudgetConsumption`` + ``rules_with_deferred_response`` tuple.

    ``evaluate_multi_rule_breach`` consumes the canonical
    ``RiskBudgetConsumption`` aggregate; the breach loop produces
    :class:`RuleEvaluation` records. The two carry compatible information
    for the multi-rule-breach criterion: zone + classification + rule_id.
    Other ``RiskBudgetEntry`` fields (headroom, units) are immaterial for
    the count-of-BLOCKED-deferred-rules test the primitive applies; we
    populate them with neutral values.

    Returns ``(consumption, rules_with_deferred_response)``. The second
    tuple is the deferred-rule registry: every unique rule_id in
    ``rule_evaluations`` whose ``classification`` is ``deferred_to_pm``.
    """
    entries: list[RiskBudgetEntry] = []
    deferred_rules: set[str] = set()
    seen_rule_ids: set[str] = set()
    for evaluation in rule_evaluations:
        rule_id = evaluation.rule_id
        if rule_id in seen_rule_ids:
            # ``RiskBudgetConsumption`` requires unique rule_ids. The
            # breach loop already produces one entry per rule (sector rules
            # have unique suffixes), so this only protects against future
            # double-emission bugs.
            continue
        seen_rule_ids.add(rule_id)
        classification = evaluation.classification or breach_response_lookup.get(
            _strip_sector_suffix(rule_id)
        )
        if classification is BreachResponse.deferred_to_pm:
            deferred_rules.add(rule_id)
        entries.append(
            RiskBudgetEntry(
                rule_id=rule_id,
                rule_label=rule_id,
                current_value=evaluation.current_value,
                limit_value=evaluation.limit_value,
                headroom=evaluation.limit_value - evaluation.current_value,
                headroom_pct_of_limit=0.0,
                zone=evaluation.zone,
                unit="pct",
                cumulative_invocation_impact_value=0.0,
            )
        )
    return RiskBudgetConsumption(entries=tuple(entries)), tuple(sorted(deferred_rules))


def _strip_sector_suffix(rule_id: str) -> str:
    """Mirror :func:`breach_loop.task._strip_sector_suffix`.

    ``sector_concentration_<sector>`` rules' breach-response classification
    lives on the bare ``sector_concentration_pct`` registry entry; this
    strips the trailing sector suffix so the lookup hits.
    """
    if rule_id.startswith("sector_concentration_") and rule_id != "sector_concentration_pct":
        return "sector_concentration_pct"
    return rule_id


class EmergencyTriggerEvaluator:
    """Composes the four trigger evaluators into the breach-loop's callback.

    The callback's contract (per story 03b's loop):

    1. Update per-session rolling windows from the new tick (regime label
       + drawdown sample).
    2. Probe the margin-call observer for a live margin call.
    3. Call :func:`evaluate_emergency_invocation` with the windows.
    4. If the primitive returns a context, check the cooldown.
       * Suppressed → log and return.
       * Allowed → build the detail, write the activity-log entry, record
         the fire.

    The evaluator is stateful (it carries the rolling windows + cooldown
    tracker) but deterministic given an input sequence.
    """

    def __init__(
        self,
        *,
        session: MonitorSession,
        breach_behavior_config: BreachBehaviorConfig,
        cooldown: CooldownTracker,
        trigger_ids: TriggerIdGenerator,
        margin_call_observer: MarginCallObserver,
        activity_log_writer: ActivityLogWriter,
        breach_response_lookup: Mapping[str, BreachResponse],
        invocation_id_provider: Callable[[], Awaitable[str]],
    ) -> None:
        self._session = session
        self._cfg = breach_behavior_config
        self._cooldown = cooldown
        self._trigger_ids = trigger_ids
        self._margin_call_observer = margin_call_observer
        self._writer = activity_log_writer
        self._breach_response_lookup = breach_response_lookup
        self._invocation_id_provider = invocation_id_provider
        # Regime window: only the prior label is needed by
        # ``evaluate_regime_jump``. Carry the last seen label only.
        self._prior_regime_label: RegimeLabel | None = None
        # Drawdown window: bounded by the velocity-window minutes plus a
        # small margin so a sample at the window boundary is retained.
        max_minutes = _max_lookback_minutes(breach_behavior_config)
        self._drawdown_window: deque[DrawdownSample] = deque()
        self._drawdown_max_lookback = timedelta(minutes=max_minutes)
        # Track the last-fire timestamp for the primitive's signature; the
        # primitive treats ``None`` as "first emergency".
        self._last_emergency_triggered_at: datetime | None = None

    async def handle_emergency_input(self, result: BreachLoopResult) -> None:
        """``on_emergency_input`` callback — see class docstring."""
        as_of = result.as_of
        current_regime = result.phase1_result.active_risk_parameters.regime_label
        # On the first tick the prior label is unknown; treat it as the
        # current label so the regime-jump primitive sees a non-tightening
        # delta and does not fire (multi-rule, velocity, and margin-call
        # checks remain live on tick 1).
        prior_regime = self._prior_regime_label or current_regime
        # Window-extend before evaluation so the new sample participates in
        # the velocity check (it can be the "after-cross" sample).
        self._extend_drawdown_window(result.drawdown_velocity_sample, as_of=as_of)

        # Probe margin-call observer once per tick.
        margin_call_event = await self._margin_call_observer()

        risk_budget, deferred_rules = _project_risk_budget(
            result.rule_evaluations,
            breach_response_lookup=self._breach_response_lookup,
        )
        # ``evaluate_emergency_invocation`` requires now > last_invocation_started_at.
        # Use ``session.started_at`` as the anchor; the primitive's check is
        # against the *previous invocation start*, but the monitor runs
        # continuously across invocations — using session start gives a
        # stable, always-positive cadence delta and the primitive uses the
        # delta only to populate ``minutes_since_last_invocation`` which the
        # detail-class does not surface.
        anchor = self._session.started_at
        if as_of <= anchor:
            anchor = as_of - timedelta(seconds=1)

        context = evaluate_emergency_invocation(
            now=as_of,
            last_invocation_started_at=anchor,
            last_emergency_triggered_at=self._last_emergency_triggered_at,
            cooldown_minutes=self._cfg.emergency_invocation_cooldown_minutes,
            normal_cadence_minutes=1.0,  # surfaced into EmergencyContext only
            prior_regime_label=prior_regime,
            current_regime_label=current_regime,
            risk_budget=risk_budget,
            rules_with_deferred_response=deferred_rules,
            drawdown_history=tuple(self._drawdown_window),
            daily_drawdown_limit_pct=self._daily_drawdown_limit_pct(result),
            margin_call_event=margin_call_event,
            config=self._cfg,
        )
        self._prior_regime_label = current_regime

        if context is None:
            return

        trigger_type = _TRIGGER_TYPE_BY_PRIMITIVE[context.trigger]

        if not self._cooldown.may_fire(now=as_of, trigger_type=trigger_type):
            log.debug(
                "emergency_trigger suppressed: session_id=%s trigger_type=%s "
                "remaining_seconds=%d reason=%s",
                self._session.session_id,
                trigger_type,
                self._cooldown.remaining_seconds(now=as_of, trigger_type=trigger_type),
                context.trigger_detail,
            )
            return

        await self._emit(
            as_of=as_of,
            trigger_type=trigger_type,
            trigger_reason=context.trigger_detail,
        )

    async def _emit(
        self,
        *,
        as_of: datetime,
        trigger_type: _TriggerType,
        trigger_reason: str,
    ) -> None:
        """Build the detail, write the entry, record the fire."""
        remaining = self._cooldown.remaining_seconds(now=as_of, trigger_type=trigger_type)
        detail = EmergencyInvocationRequestedDetail(
            trigger_type=trigger_type,
            trigger_reason=trigger_reason,
            cooldown_remaining_seconds=remaining,
        )
        trigger_id = self._trigger_ids.next()
        entry = ActivityLogEntry(
            entry_id=_format_entry_id(
                session_id=self._session.session_id,
                trigger_id=trigger_id,
            ),
            invocation_id=await self._invocation_id_provider(),
            timestamp=as_of,
            event_type=EventType.EMERGENCY_INVOCATION_REQUESTED,
            event_group=EventGroup.RISK_AND_GUARDRAIL,
            position_id=None,
            order_id=None,
            thesis_id=None,
            source=EventSource.GUARDRAIL_LAYER,
            detail=detail,
        )
        await self._writer(entry)
        self._cooldown.record_fire(now=as_of, trigger_type=trigger_type)
        self._last_emergency_triggered_at = as_of
        log.info(
            "emergency_trigger emit: session_id=%s trigger_type=%s entry_id=%s reason=%s",
            self._session.session_id,
            trigger_type,
            entry.entry_id,
            trigger_reason,
        )

    def _extend_drawdown_window(
        self,
        sample: DrawdownSample,
        *,
        as_of: datetime,
    ) -> None:
        """Append *sample* and evict entries older than the max lookback."""
        self._drawdown_window.append(sample)
        cutoff = as_of - self._drawdown_max_lookback
        while self._drawdown_window and self._drawdown_window[0].sampled_at < cutoff:
            self._drawdown_window.popleft()

    def _daily_drawdown_limit_pct(self, result: BreachLoopResult) -> float:
        """Resolve the active daily-drawdown limit from the Phase 1 result.

        The breach loop's ``Phase1EnforcementResult.active_risk_parameters``
        is the canonical source; the ``daily_drawdown_pct`` entry's value
        is the active limit. When absent, the velocity check cannot fire
        — return ``0.0`` and let the primitive's "no crossing" branch hold.
        """
        for entry in result.phase1_result.active_risk_parameters.entries:
            if entry.rule_id == "daily_drawdown_pct":
                return float(entry.value)
        return 0.0


def _format_entry_id(*, session_id: str, trigger_id: int) -> str:
    """Format a session-scoped, monotonic, collision-resistant entry_id.

    Pattern: ``mon-emt-{session_id}-{trigger_id:012d}-{nonce}``.

    * ``mon-emt-`` prefix is the continuous-monitor + emergency-trigger
      pair. The shared ``mon-`` prefix across greeks-refresh
      (``mon-grf-``), emergency-trigger (``mon-emt-``), bracket-stops
      (``mon-brk-``), and breach-loop halt transitions (``mon-alp-``)
      lets operators ``grep monitor.log | grep '^mon-'`` to find the
      union of monitor-emitted activity-log entries.
    * ``session_id`` participates so cross-session log queries pick the
      right entries.
    * ``trigger_id`` is the monotonic per-session sequence; it appears in
      the entry so operators can correlate cascade and emergency emits.
    * 4-byte random nonce guards against per-microsecond collisions in
      case a future implementation emits multiple entries in one tick.
    """
    nonce = secrets.token_hex(4)
    return f"mon-emt-{session_id}-{trigger_id:012d}-{nonce}"


__all__ = [
    "ActivityLogWriter",
    "EmergencyTriggerEvaluator",
    "MarginCallObserver",
    "NoMarginCallObserver",
]
