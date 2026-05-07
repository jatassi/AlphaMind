"""Emergency-invocation trigger evaluators and composer (story 05c).

Five public functions that evaluate the four emergency-invocation trigger
conditions (regime jump, multi-rule breach, daily-drawdown velocity, margin
call) and a composer that applies the cooldown rule, prioritises the firing
trigger, and produces an :class:`EmergencyContext`. The continuous monitor
calls :func:`evaluate_emergency_invocation` at its evaluation cadence; each
per-trigger evaluator is also exposed for narrow callers and tests.

Per ``docs/design/06-risk-guardrails/breach-behavior.md`` § *Emergency
invocation trigger*. All thresholds (60% velocity, 30-min window, 3-rule
count, 30-min cooldown) come from :class:`BreachBehaviorConfig` and the
composer's ``cooldown_minutes`` argument; this module embeds no numeric
defaults.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, field_validator

from alphamind.risk_guardrails.breach_behavior.config import BreachBehaviorConfig
from alphamind.risk_guardrails.breach_behavior.types import (
    EmergencyContext,
    EmergencyTrigger,
    RegimeLabel,
    RiskZone,
)

if TYPE_CHECKING:
    # ``RiskBudgetConsumption`` lives in ``portfolio_state.records.capital``,
    # which re-exports the four risk-guardrail enums from this package.
    # Eager import would cycle. Annotation-only usage is sound under
    # ``from __future__ import annotations``.
    from alphamind.portfolio_state.records.capital import RiskBudgetConsumption


def _require_tz_aware(v: datetime, *, field_name: str) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        msg = f"{field_name} must be timezone-aware"
        raise ValueError(msg)
    return v


class DrawdownSample(BaseModel):
    """A timestamped drawdown observation for the velocity-trigger evaluator."""

    model_config = ConfigDict(frozen=True)

    sampled_at: datetime
    intraday_drawdown_pct: float

    @field_validator("sampled_at")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _require_tz_aware(v, field_name="sampled_at")


class MarginCallEvent(BaseModel):
    """A broker-issued margin call awaiting handling."""

    model_config = ConfigDict(frozen=True)

    issued_at: datetime
    additional_margin_required_usd: float

    @field_validator("additional_margin_required_usd")
    @classmethod
    def _require_positive(cls, v: float) -> float:
        if v <= 0:
            msg = f"additional_margin_required_usd must be > 0; got {v}"
            raise ValueError(msg)
        return v

    @field_validator("issued_at")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        return _require_tz_aware(v, field_name="issued_at")


# ---------------------------------------------------------------------------
# Regime ordering — local rank for tightening-jump detection
# ---------------------------------------------------------------------------

_REGIME_INDEX: dict[RegimeLabel, int] = {
    RegimeLabel.LOW_VOL: 0,
    RegimeLabel.NORMAL: 1,
    RegimeLabel.ELEVATED: 2,
    RegimeLabel.CRISIS: 3,
}


def evaluate_regime_jump(
    *,
    prior_regime_label: RegimeLabel,
    current_regime_label: RegimeLabel,
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect whether the regime classification tightened by skipping a level.

    Regimes are ordered LOW_VOL < NORMAL < ELEVATED < CRISIS. A *tightening
    jump* fires when ``current_index - prior_index >= 2`` (skip-a-level
    tightening). Adjacent tightening transitions and any loosening transition
    do not fire.

    Returns ``(EmergencyTrigger.REGIME_JUMP, descriptive_text)`` on fire,
    else ``(None, None)``. The descriptive text is
    ``"Regime jump: {prior} -> {current}"``; the continuous monitor augments
    with VIX values when available (caller-side).

    See test anchors:
      ``test_low_vol_to_elevated_fires_regime_jump``,
      ``test_loosening_does_not_fire``,
      ``test_adjacent_tightening_does_not_fire``.
    """
    delta = _REGIME_INDEX[current_regime_label] - _REGIME_INDEX[prior_regime_label]
    if delta < 2:
        return (None, None)
    detail = f"Regime jump: {prior_regime_label.value} -> {current_regime_label.value}"
    return (EmergencyTrigger.REGIME_JUMP, detail)


def evaluate_multi_rule_breach(
    *,
    risk_budget: RiskBudgetConsumption,
    config: BreachBehaviorConfig,
    rules_with_deferred_response: tuple[str, ...],
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect 3+ deferred-response rules simultaneously breaching.

    Iterates ``risk_budget.entries`` and counts entries whose ``rule_id`` appears
    in ``rules_with_deferred_response`` and whose ``zone`` is
    ``RiskZone.BLOCKED``. Fires when the count is at or above
    ``config.multi_rule_breach_simultaneous_deferred_rules_count``.

    The deferred-rule registry is the rule registry's source of truth; this
    primitive does not duplicate it.
    """
    deferred_set = set(rules_with_deferred_response)
    breaching_ids = tuple(
        entry.rule_id
        for entry in risk_budget.entries
        if entry.rule_id in deferred_set and entry.zone == RiskZone.BLOCKED
    )
    if len(breaching_ids) < config.multi_rule_breach_simultaneous_deferred_rules_count:
        return (None, None)
    rule_list = ", ".join(breaching_ids)
    detail = (
        f"Multi-rule breach: {len(breaching_ids)} deferred rules simultaneously "
        f"over limit ({rule_list})"
    )
    return (EmergencyTrigger.MULTI_RULE_BREACH, detail)


def evaluate_daily_drawdown_velocity(
    *,
    drawdown_history: tuple[DrawdownSample, ...],
    daily_drawdown_limit_pct: float,
    config: BreachBehaviorConfig,
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect rapid daily-drawdown acceleration crossing the threshold within the window.

    Threshold value:
    ``daily_drawdown_limit_pct * config.drawdown_velocity_threshold_pct_of_daily_limit / 100.0``.

    Searches ``drawdown_history`` for the most recent sample whose drawdown is
    at or below the threshold (the "before-cross" sample) and the most recent
    sample whose drawdown is at or above the threshold (the "after-cross"
    sample). If both exist, the after-cross is later than the before-cross,
    and the time delta is at most ``config.drawdown_velocity_window_minutes``,
    the trigger fires.

    A pure crossing is required: a sample at or above threshold for the entire
    history (no earlier below-threshold sample) does not fire.
    """
    threshold_pct = (
        daily_drawdown_limit_pct * config.drawdown_velocity_threshold_pct_of_daily_limit / 100.0
    )
    sorted_history = sorted(drawdown_history, key=lambda s: s.sampled_at)

    before_cross: DrawdownSample | None = None
    after_cross: DrawdownSample | None = None
    for sample in sorted_history:
        if sample.intraday_drawdown_pct <= threshold_pct:
            before_cross = sample
            after_cross = None
        elif before_cross is not None:
            after_cross = sample

    if before_cross is None or after_cross is None:
        return (None, None)

    minutes_elapsed = (after_cross.sampled_at - before_cross.sampled_at).total_seconds() / 60.0
    if minutes_elapsed > config.drawdown_velocity_window_minutes:
        return (None, None)

    detail = (
        f"Daily drawdown velocity: crossed {threshold_pct:g}% of daily limit "
        f"({after_cross.intraday_drawdown_pct:g}% drawdown) within "
        f"{minutes_elapsed:g} minutes "
        f"(window: {config.drawdown_velocity_window_minutes} min)"
    )
    return (EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY, detail)


def evaluate_margin_call(
    *,
    margin_call_event: MarginCallEvent | None,
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect a broker-issued margin call.

    Trivial evaluator: when ``margin_call_event`` is non-None the trigger fires
    and the descriptive text names the dollar amount; otherwise it does not.

    The composer treats this trigger specially per the design's cooldown
    exception (margin calls bypass the 30-minute cooldown).
    """
    if margin_call_event is None:
        return (None, None)
    detail = (
        f"Margin call: ${margin_call_event.additional_margin_required_usd:,.2f} "
        "additional margin required"
    )
    return (EmergencyTrigger.MARGIN_CALL, detail)


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def evaluate_emergency_invocation(  # noqa: PLR0913 — signature dictated by story 05c AC composer surface
    *,
    now: datetime,
    last_invocation_started_at: datetime,
    last_emergency_triggered_at: datetime | None,
    cooldown_minutes: int,
    normal_cadence_minutes: float,
    prior_regime_label: RegimeLabel,
    current_regime_label: RegimeLabel,
    risk_budget: RiskBudgetConsumption,
    rules_with_deferred_response: tuple[str, ...],
    drawdown_history: tuple[DrawdownSample, ...],
    daily_drawdown_limit_pct: float,
    margin_call_event: MarginCallEvent | None,
    config: BreachBehaviorConfig,
) -> EmergencyContext | None:
    """Evaluate all four triggers and return an :class:`EmergencyContext` on fire.

    Cooldown rule (per ``breach-behavior.md`` § *Emergency invocation trigger*):
    margin call always fires. Other triggers fire only when
    ``last_emergency_triggered_at`` is None (first emergency of the session) or
    when ``now - last_emergency_triggered_at`` is at least
    ``cooldown_minutes``.

    Priority order (highest first):
      1. ``MARGIN_CALL``
      2. ``REGIME_JUMP``
      3. ``DAILY_DRAWDOWN_VELOCITY``
      4. ``MULTI_RULE_BREACH``

    The first firing trigger in priority order populates the returned
    :class:`EmergencyContext`; subsequent firings coalesce into the in-progress
    emergency at the caller (this primitive returns one trigger per call).

    Returns the context on fire; ``None`` when no eligible trigger fires.

    Raises:
        ValueError: when ``now`` is timezone-naive, when
            ``last_invocation_started_at`` is timezone-naive, or when
            ``now <= last_invocation_started_at`` (zero or negative cadence
            delta — clock skew or scheduler bug).
    """
    _require_tz_aware(now, field_name="now")
    _require_tz_aware(last_invocation_started_at, field_name="last_invocation_started_at")
    if now <= last_invocation_started_at:
        msg = (
            "now must be after last_invocation_started_at; "
            f"got now={now.isoformat()} and last_invocation_started_at="
            f"{last_invocation_started_at.isoformat()}"
        )
        raise ValueError(msg)

    minutes_since_last_invocation = (now - last_invocation_started_at).total_seconds() / 60.0

    cooldown_active = (
        last_emergency_triggered_at is not None
        and (now - last_emergency_triggered_at).total_seconds() / 60.0 < cooldown_minutes
    )

    def _context(trigger: EmergencyTrigger, detail: str) -> EmergencyContext:
        return EmergencyContext(
            trigger=trigger,
            trigger_detail=detail,
            minutes_since_last_invocation=minutes_since_last_invocation,
            normal_cadence_minutes=normal_cadence_minutes,
        )

    # Priority 1: margin call (bypasses cooldown).
    margin_trigger, margin_detail = evaluate_margin_call(
        margin_call_event=margin_call_event,
    )
    if margin_trigger is not None and margin_detail is not None:
        return _context(margin_trigger, margin_detail)

    if cooldown_active:
        return None

    # Priority 2: regime jump.
    regime_trigger, regime_detail = evaluate_regime_jump(
        prior_regime_label=prior_regime_label,
        current_regime_label=current_regime_label,
    )
    if regime_trigger is not None and regime_detail is not None:
        return _context(regime_trigger, regime_detail)

    # Priority 3: daily drawdown velocity.
    velocity_trigger, velocity_detail = evaluate_daily_drawdown_velocity(
        drawdown_history=drawdown_history,
        daily_drawdown_limit_pct=daily_drawdown_limit_pct,
        config=config,
    )
    if velocity_trigger is not None and velocity_detail is not None:
        return _context(velocity_trigger, velocity_detail)

    # Priority 4: multi-rule breach.
    multi_trigger, multi_detail = evaluate_multi_rule_breach(
        risk_budget=risk_budget,
        config=config,
        rules_with_deferred_response=rules_with_deferred_response,
    )
    if multi_trigger is not None and multi_detail is not None:
        return _context(multi_trigger, multi_detail)

    return None
