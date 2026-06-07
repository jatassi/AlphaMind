"""Programmatic component-outcome assessor — ALP-897.

Pure function (no DB / LLM I/O). Resolves falsifiable quantitative thesis
components to ThesisComponentOutcome using only the component's type and the
closed position's exit method. Qualitative / ambiguous components — including
all ENTRY_RATIONALE components and any component whose outcome cannot be
mechanically determined from the exit method — return INCONCLUSIVE. The
targeted LLM evaluator (story 04d) refines those.

Mapping:

    TARGET_RATIONALE:
        TARGET_REACHED              → VALIDATED   (target was hit)
        STOP_TRIGGERED              → WRONG       (stopped out before target)
        TIME_EXPIRED                → WRONG       (timed out before target)
        PM_DECISION / MARGIN_LIQUIDATION /
        FORCED_BUY_IN / CORP_ACTION → INCONCLUSIVE (closed for external reasons)

    INVALIDATION_RATIONALE:
        STOP_TRIGGERED              → WRONG       (invalidation condition fired)
        TIME_EXPIRED                → WRONG       (time-bound invalidation fired)
        all other exits             → INCONCLUSIVE (exited for a different reason;
                                                    can't confirm invalidation was met)

    ENTRY_RATIONALE:
        (any exit)                  → INCONCLUSIVE (qualitative — LLM evaluator)
"""

from __future__ import annotations

from alphamind.portfolio_state.events.activity_log import PositionExitMethod
from alphamind.portfolio_state.records.theses import (
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
)

# Exit methods that mechanically indicate the thesis target was not reached
# (the position was forced out by a stop or time limit rather than hitting
# the take-profit level).
_TARGET_MISS_EXITS: frozenset[PositionExitMethod] = frozenset(
    {
        PositionExitMethod.STOP_TRIGGERED,
        PositionExitMethod.TIME_EXPIRED,
    }
)

# Exit methods that mechanically indicate an invalidation condition fired.
_INVALIDATION_TRIGGER_EXITS: frozenset[PositionExitMethod] = frozenset(
    {
        PositionExitMethod.STOP_TRIGGERED,
        PositionExitMethod.TIME_EXPIRED,
    }
)


def assess_component_programmatically(
    component: ThesisComponent,
    exit_method: PositionExitMethod,
) -> ThesisComponentOutcome:
    """Assess a single thesis component's outcome programmatically.

    Pure: no I/O, no LLM calls. Takes the component's type and the closed
    position's exit method, returns the deterministic outcome where one can
    be established, or INCONCLUSIVE otherwise.

    Args:
        component: The thesis component to assess.
        exit_method: How the position was closed (from the activity log).

    Returns:
        ThesisComponentOutcome — VALIDATED, WRONG, or INCONCLUSIVE.
    """
    match component.component_type:
        case ThesisComponentType.TARGET_RATIONALE:
            if exit_method is PositionExitMethod.TARGET_REACHED:
                return ThesisComponentOutcome.VALIDATED
            if exit_method in _TARGET_MISS_EXITS:
                return ThesisComponentOutcome.WRONG
            return ThesisComponentOutcome.INCONCLUSIVE

        case ThesisComponentType.INVALIDATION_RATIONALE:
            if exit_method in _INVALIDATION_TRIGGER_EXITS:
                return ThesisComponentOutcome.WRONG
            return ThesisComponentOutcome.INCONCLUSIVE

        case ThesisComponentType.ENTRY_RATIONALE:
            # Qualitative — can only be assessed by the LLM evaluator.
            return ThesisComponentOutcome.INCONCLUSIVE
