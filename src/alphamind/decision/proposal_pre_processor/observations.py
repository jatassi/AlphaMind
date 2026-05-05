"""Aggregate histogram functions for the proposal pre-processor — ALP-315.

Pure functions that compute the conviction distribution and book health summary
from analyst recommendations and strategist position assessments respectively.
"""

from __future__ import annotations

from collections.abc import Sequence

from alphamind.decision.analyst.models import Recommendation
from alphamind.decision.proposal_pre_processor.models import (
    BookHealthSummary,
    ByRecommendedAction,
    ByThesisStatus,
    ConvictionDistribution,
    ConvictionHistogram,
)
from alphamind.decision.strategist.models import PositionAssessment

__all__ = [
    "compute_book_health_summary",
    "compute_conviction_distribution",
]


def compute_conviction_distribution(
    recommendations: Sequence[Recommendation],
) -> ConvictionDistribution:
    """Histogram analyst recommendations by conviction level (1 through 5).

    Watchlist mode: caller passes an empty sequence; result is all zeros with total=0.
    """
    counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for rec in recommendations:
        level = rec.conviction_level
        if level not in counts:
            raise ValueError(f"conviction_level {level!r} is outside the valid range 1..5")
        counts[level] += 1

    return ConvictionDistribution(
        by_level=ConvictionHistogram(
            **{
                "1": counts[1],
                "2": counts[2],
                "3": counts[3],
                "4": counts[4],
                "5": counts[5],
            }
        ),
        total=len(recommendations),
    )


def compute_book_health_summary(
    position_assessments: Sequence[PositionAssessment],
) -> BookHealthSummary:
    """Histogram strategist position assessments by thesis_status and recommended_action.

    Empty book: caller passes an empty sequence; result is all zeros,
    remedy_flagged_count=0, total=0.
    """
    thesis_counts: dict[str, int] = {
        "on-track": 0,
        "partially-realized": 0,
        "at-risk": 0,
        "stale": 0,
        "invalidated": 0,
    }
    action_counts: dict[str, int] = {
        "hold": 0,
        "reduce": 0,
        "close": 0,
        "adjust-bracket": 0,
        "add": 0,
    }
    remedy_flagged_count = 0

    for assessment in position_assessments:
        thesis_counts[assessment.thesis_status] += 1
        action_counts[assessment.recommended_action] += 1
        if bool(assessment.remedy_flag):
            remedy_flagged_count += 1

    return BookHealthSummary(
        by_thesis_status=ByThesisStatus(
            **{
                "on-track": thesis_counts["on-track"],
                "partially-realized": thesis_counts["partially-realized"],
                "at-risk": thesis_counts["at-risk"],
                "stale": thesis_counts["stale"],
                "invalidated": thesis_counts["invalidated"],
            }
        ),
        by_recommended_action=ByRecommendedAction(
            **{
                "hold": action_counts["hold"],
                "reduce": action_counts["reduce"],
                "close": action_counts["close"],
                "adjust-bracket": action_counts["adjust-bracket"],
                "add": action_counts["add"],
            }
        ),
        remedy_flagged_count=remedy_flagged_count,
        total=len(position_assessments),
    )
