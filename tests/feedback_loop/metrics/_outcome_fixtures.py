"""Hand-built outcome fixtures for the feedback-loop outcome-tier metric tests.

Resolved theses do not exist in production until story 04e (thesis-resolution
authoring) lands, so the outcome metrics are driven entirely by hand-built
fixtures: a resolved :class:`ThesisRecord` builder (for the read-helper test that
encodes records into rows) and a :class:`ThesisOutcome` builder carrying the
conditioning attributes the conditioning surface slices on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alphamind._kernel.ids import PositionId, ThesisId
from alphamind.feedback_loop.dataset import ConditioningAttributes, ThesisOutcome
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)

_GEN = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_TIME_EXPECTATION_HOURS = 24.0


def make_resolved_thesis_record(
    thesis_id: str,
    position_id: str,
    *,
    resolution_timestamp: datetime,
    resolution_category: ThesisResolutionCategory = ThesisResolutionCategory.VALIDATED,
    resolution_pnl_usd: float = 100.0,
) -> ThesisRecord:
    """A fully-populated RESOLVED ``ThesisRecord`` with the three mandatory components."""
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ctype.value}",
            thesis_id=ThesisId(thesis_id),
            component_type=ctype,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative=f"{ctype.value} narrative",
            key_assumptions=(KeyAssumption(text="Earnings beat", outcome=None),),
            generation_timestamp=_GEN,
            resolution_outcome=ThesisComponentOutcome.VALIDATED,
            resolution_notes=None,
        )
        for ctype in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="AAPL momentum",
        key_catalyst="Q3 earnings beat",
        position_size_rationale="Sized at 5% conviction-3",
        components=components,
        status=ThesisRecordStatus.RESOLVED,
        generation_timestamp=_GEN,
        time_expectation_hours=_TIME_EXPECTATION_HOURS,
        age_hours=4.0,
        expected_resolution_at=_GEN + timedelta(hours=_TIME_EXPECTATION_HOURS),
        resolution_timestamp=resolution_timestamp,
        resolution_category=resolution_category,
        resolution_pnl_usd=resolution_pnl_usd,
        entry_fill_gap_usd=None,
    )


def make_thesis_outcome(
    *,
    thesis_id: str = "thes-1",
    resolution_category: ThesisResolutionCategory = ThesisResolutionCategory.VALIDATED,
    resolution_pnl_usd: float = 100.0,
    active_duration_hours: float = 20.0,
    expected_duration_hours: float = 24.0,
    regime: str | None = None,
    sector: str | None = None,
    conviction: str | None = None,
    strategist_status: str | None = None,
    anti_patterns: tuple[str, ...] = (),
    time_of_day: str | None = None,
    prompt_version: str | None = None,
    model_version: str | None = None,
) -> ThesisOutcome:
    """A single resolved-thesis outcome observation with conditioning attributes."""
    return ThesisOutcome(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(f"pos-{thesis_id}"),
        resolution_category=resolution_category,
        resolution_pnl_usd=resolution_pnl_usd,
        active_duration_hours=active_duration_hours,
        expected_duration_hours=expected_duration_hours,
        conditioning=ConditioningAttributes(
            regime=regime,
            sector=sector,
            conviction=conviction,
            strategist_status=strategist_status,
            anti_patterns=anti_patterns,
            time_of_day=time_of_day,
            prompt_version=prompt_version,
            model_version=model_version,
        ),
    )
