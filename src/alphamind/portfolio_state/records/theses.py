"""Consumer-facing typed records for the thesis registry (story 03b)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from alphamind.portfolio_state.records.orders import BracketLegType


class ThesisStatus(StrEnum):
    ON_TRACK = "ON_TRACK"
    PARTIALLY_REALIZED = "PARTIALLY_REALIZED"
    AT_RISK = "AT_RISK"
    STALE = "STALE"
    INVALIDATED = "INVALIDATED"


class ThesisRecordStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class ThesisComponentType(StrEnum):
    ENTRY_RATIONALE = "ENTRY_RATIONALE"
    TARGET_RATIONALE = "TARGET_RATIONALE"
    INVALIDATION_RATIONALE = "INVALIDATION_RATIONALE"


class ThesisResolutionCategory(StrEnum):
    VALIDATED = "VALIDATED"
    PROFITABLE_BUT_WRONG = "PROFITABLE_BUT_WRONG"
    INVALIDATED_STOPPED_CORRECTLY = "INVALIDATED_STOPPED_CORRECTLY"
    INVALIDATED_WRONG_ON_EXIT = "INVALIDATED_WRONG_ON_EXIT"
    CANCELLED_NEVER_ENTERED = "CANCELLED_NEVER_ENTERED"


class ThesisComponentOutcome(StrEnum):
    VALIDATED = "VALIDATED"
    WRONG = "WRONG"
    INCONCLUSIVE = "INCONCLUSIVE"


class SupportingSignalStatus(StrEnum):
    PRESENT = "PRESENT"
    STRENGTHENED = "STRENGTHENED"
    WEAKENED = "WEAKENED"
    REVERSED = "REVERSED"


class KeyAssumption(BaseModel):
    """A short structured falsifiable claim within a thesis component."""

    model_config = ConfigDict(frozen=True)

    text: str
    outcome: ThesisComponentOutcome | None


class SupportingSignal(BaseModel):
    """A named signal and its current strategist-assessed status."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: SupportingSignalStatus


class ThesisComponent(BaseModel):
    """A typed, individually-addressable component linked to a specific order or bracket leg."""

    model_config = ConfigDict(frozen=True)

    component_id: str
    thesis_id: str
    component_type: ThesisComponentType
    linked_bracket_leg_type: BracketLegType | None
    linked_bracket_leg_id: str | None = None
    instrument_reference: str
    narrative: str
    key_assumptions: tuple[KeyAssumption, ...]
    supporting_signals: tuple[SupportingSignal, ...]
    generation_timestamp: datetime
    resolution_outcome: ThesisComponentOutcome | None
    resolution_notes: str | None


class ThesisRecord(BaseModel):
    """Full consumer-facing thesis record, one-to-one with a position."""

    model_config = ConfigDict(frozen=True)

    thesis_id: str
    position_id: str
    summary: str
    components: tuple[ThesisComponent, ...]
    status: ThesisRecordStatus
    health_status: ThesisStatus | None
    prior_health_status: ThesisStatus | None
    generation_timestamp: datetime
    time_expectation_hours: str
    age_hours: float
    expected_resolution_at: datetime
    resolution_timestamp: datetime | None
    resolution_category: ThesisResolutionCategory | None
    resolution_pnl_usd: float | None
    entry_fill_gap_usd: float | None
    key_catalyst: str

    @model_validator(mode="after")
    def _validate_all(self) -> ThesisRecord:
        self._check_summary_non_empty()
        self._check_time_expectation_non_empty()
        self._check_age_hours()
        self._check_components_non_empty_for_active_resolved()
        self._check_mandatory_coverage()
        self._check_active_resolution_fields()
        self._check_resolved_fields()
        return self

    def _check_summary_non_empty(self) -> None:
        if not self.summary:
            msg = "summary must be a non-empty string"
            raise ValueError(msg)

    def _check_time_expectation_non_empty(self) -> None:
        if not self.time_expectation_hours:
            msg = "time_expectation_hours must be a non-empty string"
            raise ValueError(msg)

    def _check_age_hours(self) -> None:
        if self.age_hours < 0:
            msg = f"age_hours must be >= 0; got {self.age_hours}"
            raise ValueError(msg)

    def _check_components_non_empty_for_active_resolved(self) -> None:
        active_or_resolved = self.status in (ThesisRecordStatus.ACTIVE, ThesisRecordStatus.RESOLVED)
        if active_or_resolved and not self.components:
            msg = f"components must be non-empty for status={self.status}"
            raise ValueError(msg)

    def _check_mandatory_coverage(self) -> None:
        if self.status == ThesisRecordStatus.CANCELLED:
            return
        types = {c.component_type for c in self.components}
        missing = [
            t.value
            for t in (
                ThesisComponentType.ENTRY_RATIONALE,
                ThesisComponentType.TARGET_RATIONALE,
                ThesisComponentType.INVALIDATION_RATIONALE,
            )
            if t not in types
        ]
        if missing:
            msg = f"components missing required types: {missing}"
            raise ValueError(msg)

    def _check_active_resolution_fields(self) -> None:
        if self.status != ThesisRecordStatus.ACTIVE:
            return
        if self.resolution_timestamp is not None:
            msg = "ACTIVE thesis must have resolution_timestamp as None"
            raise ValueError(msg)
        if self.resolution_category is not None:
            msg = "ACTIVE thesis must have resolution_category as None"
            raise ValueError(msg)
        if self.resolution_pnl_usd is not None:
            msg = "ACTIVE thesis must have resolution_pnl_usd as None"
            raise ValueError(msg)

    def _check_resolved_fields(self) -> None:
        if self.status != ThesisRecordStatus.RESOLVED:
            return
        if self.resolution_timestamp is None:
            msg = "RESOLVED thesis must have resolution_timestamp non-None"
            raise ValueError(msg)
        if self.resolution_category is None:
            msg = "RESOLVED thesis must have resolution_category non-None"
            raise ValueError(msg)
        if self.resolution_pnl_usd is None:
            msg = "RESOLVED thesis must have resolution_pnl_usd non-None"
            raise ValueError(msg)
        unresolved = [c.component_id for c in self.components if c.resolution_outcome is None]
        if unresolved:
            msg = f"RESOLVED thesis has components with resolution_outcome=None: {unresolved}"
            raise ValueError(msg)


class RecentThesisResolution(BaseModel):
    """Compact projection for the rolling-window delivery in category 3c."""

    model_config = ConfigDict(frozen=True)

    thesis_id: str
    position_id: str
    resolution_category: ThesisResolutionCategory
    component_outcomes: tuple[tuple[str, ThesisComponentOutcome], ...]
    resolution_pnl_usd: float
    active_duration_hours: float
    expected_duration_hours: float
    signal_post_mortem: str | None
