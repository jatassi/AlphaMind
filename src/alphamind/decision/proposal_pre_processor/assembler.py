"""Bundle assembly internals — ALP-318.

Pure helpers that build the strategist and analyst sections of the
:class:`ProposalPreProcessorBundle`. The public entry point lives in
:mod:`alphamind.decision.proposal_pre_processor.runner`; this module's
helpers are private but stable enough to test directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.proposal_pre_processor.conflicts import (
    ConflictDetectionResult,
)
from alphamind.decision.proposal_pre_processor.models import (
    AnalystSection,
    AnalystSideAnnotations,
    StrategistSection,
    StrategistSideAnnotations,
    WrappedPendingOrderAssessment,
    WrappedPositionAssessment,
    WrappedRecommendation,
)
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.risk_guardrails.guardrail_evaluation import Direction, PortfolioStateSnapshot

__all__ = [
    "BundleAssemblyError",
    "build_analyst_section",
    "build_held_direction_resolver",
    "build_strategist_section",
    "verify_halt_state_consistency",
    "verify_invocation_id_consistency",
]


class BundleAssemblyError(Exception):
    """Raised when the runner detects a structurally invalid input combination.

    Per design, halt-state mode disagreement and invocation_id mismatch
    indicate fixture or pipeline-composition bugs — they should never reach
    the pre-processor from a valid pipeline run.
    """


def verify_halt_state_consistency(
    analyst_output: AnalystOutput, strategist_output: StrategistOutput
) -> None:
    """Raise ``BundleAssemblyError`` if analyst/strategist halt-modes disagree.

    Halt is system-wide: ``analyst.mode == "watchlist"`` iff
    ``strategist.mode == "defensive_posture"``.
    """
    analyst_halted = analyst_output.mode == "watchlist"
    strategist_halted = strategist_output.mode == "defensive_posture"
    if analyst_halted != strategist_halted:
        raise BundleAssemblyError(
            f"halt-state inconsistency: analyst.mode={analyst_output.mode!r}, "
            f"strategist.mode={strategist_output.mode!r}"
        )


def verify_invocation_id_consistency(
    analyst_output: AnalystOutput, strategist_output: StrategistOutput
) -> None:
    """Raise ``BundleAssemblyError`` if the two outputs report different invocation IDs."""
    if analyst_output.invocation_id != strategist_output.invocation_id:
        raise BundleAssemblyError(
            f"invocation_id mismatch: analyst={analyst_output.invocation_id!r}, "
            f"strategist={strategist_output.invocation_id!r}"
        )


def build_held_direction_resolver(
    snapshot: PortfolioStateSnapshot,
) -> Callable[[str], Literal["long", "short"]]:
    """Build a resolver from snapshot existing-positions for conflict detection."""

    def _resolver(position_id: str) -> Literal["long", "short"]:
        position = snapshot.existing_positions[position_id]
        return "long" if position.direction is Direction.LONG else "short"

    return _resolver


def build_strategist_section(
    strategist_output: StrategistOutput,
    conflicts: ConflictDetectionResult,
) -> StrategistSection:
    """Wrap each strategist record with its pre-computed conflict annotations.

    Preserves the strategist's emitted order verbatim. The watchlist-mode
    caller passes a ``ConflictDetectionResult`` with empty maps; the
    ``.get(..., ())`` calls then yield empty conflict tuples uniformly.
    """
    wrapped_positions = tuple(
        WrappedPositionAssessment(
            assessment=assessment,
            pre_processor_annotations=StrategistSideAnnotations(
                conflicts=conflicts.strategist_position_conflicts_by_assessment_id.get(
                    assessment.assessment_id, ()
                ),
            ),
        )
        for assessment in strategist_output.position_assessments
    )
    wrapped_pendings = tuple(
        WrappedPendingOrderAssessment(
            pending_order_assessment=pending,
            pre_processor_annotations=StrategistSideAnnotations(
                conflicts=conflicts.strategist_pending_conflicts_by_assessment_id.get(
                    pending.pending_order_assessment_id, ()
                ),
            ),
        )
        for pending in strategist_output.pending_order_assessments
    )
    return StrategistSection(
        mode=strategist_output.mode,
        position_assessments=wrapped_positions,
        pending_order_assessments=wrapped_pendings,
        portfolio_level_observations=strategist_output.portfolio_level_observations,
    )


def build_analyst_section(
    analyst_output: AnalystOutput,
    conflicts: ConflictDetectionResult,
) -> AnalystSection:
    """Build §3 with mode-conditional shape.

    Normal mode: each recommendation is wrapped with its conflict annotations,
    preserving the analyst's emitted order. Watchlist mode: watchlist entries
    pass through verbatim (no wrap, no annotations); ``recommendations`` is None.
    """
    if analyst_output.mode == "watchlist":
        # AnalystOutput's mode-conditional invariant guarantees watchlist is set.
        assert analyst_output.watchlist is not None
        return AnalystSection(
            mode="watchlist",
            watchlist=analyst_output.watchlist,
        )
    assert analyst_output.recommendations is not None
    wrapped = tuple(
        WrappedRecommendation(
            recommendation=rec,
            pre_processor_annotations=AnalystSideAnnotations(
                conflicts=conflicts.analyst_conflicts_by_rec_id.get(rec.recommendation_id, ()),
            ),
        )
        for rec in analyst_output.recommendations
    )
    return AnalystSection(mode="normal", recommendations=wrapped)
