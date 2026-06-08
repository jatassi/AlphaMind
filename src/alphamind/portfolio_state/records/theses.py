"""Consumer-facing typed records for the thesis registry (story 03b)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId
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


class ThesisNature(StrEnum):
    """Whether the thesis is invalidated by an underlying move or by option PnL.

    The persisted counterpart of the wire ``Thesis.nature`` tag (ALP-848 /
    ADR-0003). A ``DIRECTIONAL`` thesis is invalidated by an underlying price
    level (a long call on an up-move); a ``NON_DIRECTIONAL`` vol / spread thesis
    has no single invalidating underlying level — its PnL is nonlinear in the
    underlying. The nature determines which signal the thesis-invalidation stop
    fires on (see ``BracketLeg.trigger_signal``), selected by the continuous
    monitor (ALP-852).
    """

    DIRECTIONAL = "DIRECTIONAL"
    NON_DIRECTIONAL = "NON_DIRECTIONAL"


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


@dataclass(frozen=True, slots=True)
class KeyAssumption:
    """A short structured falsifiable claim within a thesis component."""

    text: str
    outcome: ThesisComponentOutcome | None


@dataclass(frozen=True, slots=True)
class SupportingSignal:
    """A named signal and its current strategist-assessed status."""

    name: str
    status: SupportingSignalStatus


@dataclass(frozen=True, slots=True)
class ThesisComponent:
    """A typed, individually-addressable component linked to a specific order or bracket leg.

    Carries entry-time component data only. Per-invocation supporting-signal
    re-assessment lives on
    :class:`alphamind.portfolio_state.views.thesis_health.ComponentHealthEntry`.
    """

    component_id: str
    thesis_id: ThesisId
    component_type: ThesisComponentType
    linked_bracket_leg_type: BracketLegType | None
    instrument_reference: str
    narrative: str
    key_assumptions: tuple[KeyAssumption, ...]
    generation_timestamp: datetime
    resolution_outcome: ThesisComponentOutcome | None
    resolution_notes: str | None
    linked_bracket_leg_id: str | None = None


@dataclass(frozen=True, slots=True)
class ThesisRecord:
    """Full consumer-facing thesis record, one-to-one with a position.

    Carries entry-time + lifecycle thesis data only. Per-invocation
    health-status re-assessment lives on
    :class:`alphamind.portfolio_state.views.thesis_health.ThesisHealthSnapshot`.
    """

    thesis_id: ThesisId
    position_id: PositionId
    summary: str
    key_catalyst: str
    components: tuple[ThesisComponent, ...]
    status: ThesisRecordStatus
    generation_timestamp: datetime
    time_expectation_hours: float
    age_hours: float
    expected_resolution_at: datetime
    resolution_timestamp: datetime | None
    resolution_category: ThesisResolutionCategory | None
    resolution_pnl_usd: float | None
    entry_fill_gap_usd: float | None
    # ALP-852 / ADR-0003 — the thesis-shape tag (directional vs non-directional)
    # the continuous monitor reads to select the thesis-invalidation stop's
    # trigger signal. Defaults to DIRECTIONAL: an underlying-triggered stop is
    # the legacy uniform behaviour and the conservative default for any record
    # constructed before the OPEN command carried a nature tag.
    nature: ThesisNature = ThesisNature.DIRECTIONAL
    # The analyst's prose rationale for *why this size at this conviction*.
    # Read by the PM's sizing-proportionality evaluation criterion.
    # Persisted from the analyst's proposal at OPEN time; not modified by
    # ADJUST or ADD (those have their own per-action rationale fields).
    position_size_rationale: str | None = None
    # The invocation that generated this thesis (ALP-919 / story 02i).
    # Stamped at OPEN write-back time; NULL for theses created before the
    # column existed or before THESIS_CREATED emission landed.
    # Back-populated via migration from the THESIS_CREATED activity_log entry.
    invocation_id: InvocationId | None = None

    def __post_init__(self) -> None:
        if self.position_size_rationale is not None and not self.position_size_rationale.strip():
            msg = "position_size_rationale must be non-empty when not None"
            raise ValueError(msg)
        if self.time_expectation_hours <= 0:
            msg = f"time_expectation_hours must be > 0; got {self.time_expectation_hours}"
            raise ValueError(msg)
        self._check_summary_non_empty()
        self._check_time_expectation_consistency()
        self._check_age_hours()
        self._check_components_non_empty_for_active_resolved()
        self._check_mandatory_coverage()
        self._check_active_resolution_fields()
        self._check_resolved_fields()

    def _check_summary_non_empty(self) -> None:
        if not self.summary:
            msg = "summary must be a non-empty string"
            raise ValueError(msg)

    def _check_time_expectation_consistency(self) -> None:
        """Validate expected_resolution_at is consistent with time_expectation_hours within 60s.

        Allows up to 60 seconds of drift for fractional-hour rounding in producers.
        """
        expected = self.generation_timestamp + timedelta(hours=self.time_expectation_hours)
        delta_seconds = abs((self.expected_resolution_at - expected).total_seconds())
        if delta_seconds > 60.0:
            msg = (
                f"expected_resolution_at ({self.expected_resolution_at!r}) must equal "
                f"generation_timestamp ({self.generation_timestamp!r}) + "
                f"time_expectation_hours ({self.time_expectation_hours}) within 60s; "
                f"got delta of {delta_seconds:.1f}s"
            )
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


@dataclass(frozen=True, slots=True)
class RecentThesisResolution:
    """Compact projection for the rolling-window delivery in category 3c."""

    thesis_id: ThesisId
    position_id: PositionId
    resolution_category: ThesisResolutionCategory
    component_outcomes: tuple[tuple[str, ThesisComponentOutcome], ...]
    resolution_pnl_usd: float
    active_duration_hours: float
    expected_duration_hours: float
    signal_post_mortem: str | None
