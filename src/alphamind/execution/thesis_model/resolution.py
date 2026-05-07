"""Thesis-level resolution classifier — ALP-334."""

from __future__ import annotations

from alphamind.portfolio_state.events.activity_log import PositionExitMethod
from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisResolutionCategory,
)

# Exits driven by mechanical rules rather than PM judgment.
_MECHANICAL_EXIT_METHODS: frozenset[PositionExitMethod] = frozenset(
    {
        PositionExitMethod.STOP_TRIGGERED,
        PositionExitMethod.TARGET_REACHED,
        PositionExitMethod.TIME_EXPIRED,
        PositionExitMethod.MARGIN_LIQUIDATION,
        PositionExitMethod.FORCED_BUY_IN,
        PositionExitMethod.CORPORATE_ACTION_CASH_MERGER,
    }
)


def classify_thesis_resolution(
    component_outcomes: tuple[ThesisComponentOutcome, ...],
    realized_pnl_usd: float,
    exit_method: PositionExitMethod,
) -> ThesisResolutionCategory:
    """Classify a RESOLVED thesis into one of four resolution categories.

    Mapping rules per docs/design/05-execution-layer/thesis-model.md:

    * VALIDATED — most components VALIDATED AND realized_pnl_usd > 0.
      Thesis played out, catalyst fired, position hit the target. The ideal
      outcome — reasoning was sound.

    * PROFITABLE_BUT_WRONG — realized_pnl_usd > 0 but most components are
      WRONG or INCONCLUSIVE. Lucky outcome that shouldn't reinforce the
      reasoning patterns that produced it.

    * INVALIDATED_STOPPED_CORRECTLY — realized_pnl_usd <= 0 AND exit_method
      indicates a mechanically-triggered exit (stop-triggered, time-expired,
      target-reached, margin-liquidation, forced-buy-in, corporate_action_*).
      Negative P/L outcome but positive process outcome — the system correctly
      identified when it was wrong.

    * INVALIDATED_WRONG_ON_EXIT — realized_pnl_usd <= 0 AND exit_method
      indicates a PM-judgment exit (pm-decision). Process failure — system
      held too long against an already-wrong thesis until the PM manually
      closed.

    The "most components" predicate counts VALIDATED vs WRONG; INCONCLUSIVE
    components do not weigh either side. A tie (equal validated and wrong
    counts) classifies as PROFITABLE_BUT_WRONG when P/L > 0 (the validated
    case requires affirmative weight) and INVALIDATED_STOPPED_CORRECTLY or
    INVALIDATED_WRONG_ON_EXIT (per exit_method) when P/L <= 0.

    Raises ValueError if component_outcomes is empty (a resolved thesis must
    have ≥1 resolved component per ThesisRecord._check_resolved_fields).

    Does NOT produce CANCELLED_NEVER_ENTERED — that category is set by the
    persistence layer on CANCELLED-status theses (entry order cancelled before
    fill), not by this classifier which handles RESOLVED-status theses only.
    """
    if not component_outcomes:
        msg = "component_outcomes must be non-empty; a resolved thesis has ≥1 component"
        raise ValueError(msg)

    if realized_pnl_usd > 0:
        validated_count = sum(o == ThesisComponentOutcome.VALIDATED for o in component_outcomes)
        wrong_count = sum(o == ThesisComponentOutcome.WRONG for o in component_outcomes)
        if validated_count > wrong_count:
            return ThesisResolutionCategory.VALIDATED
        return ThesisResolutionCategory.PROFITABLE_BUT_WRONG

    # realized_pnl_usd <= 0 — VALIDATED requires affirmative P/L > 0, so partition by
    # exit method only, regardless of component outcomes.
    if exit_method in _MECHANICAL_EXIT_METHODS:
        return ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY
    return ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT
