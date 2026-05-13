"""Bracket event details — activation, completion, modification, cancellation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alphamind.portfolio_state.events.types import (
    BracketModificationSource,
    EventGroup,
    EventType,
)


@dataclass(frozen=True, slots=True)
class BracketActivatedDetail:
    """Detail payload for BRACKET_ACTIVATED events."""

    bracket_id: str
    protective_leg_order_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BracketCompletedDetail:
    """Detail payload for BRACKET_COMPLETED events."""

    triggered_leg_id: str
    fill_details_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BracketDissolvedDetail:
    """Detail payload for BRACKET_DISSOLVED events."""

    cancelled_leg_order_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BracketModifiedDetail:
    """Detail payload for BRACKET_MODIFIED events."""

    source: BracketModificationSource
    field_changed: str
    old_value: str
    new_value: str
    rationale: str | None

    def __post_init__(self) -> None:
        if (
            self.source != BracketModificationSource.FILL_ANCHOR_RECALCULATION
            and self.rationale is None
        ):
            msg = "rationale must be non-None when source is not FILL_ANCHOR_RECALCULATION"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BracketIncompleteWarningDetail:
    """Detail payload for BRACKET_INCOMPLETE_WARNING events."""

    missing_leg_types: tuple[str, ...]
    expected_resolution: str


@dataclass(frozen=True, slots=True)
class BracketCancelledCorporateActionDetail:
    """Detail payload for BRACKET_CANCELLED_CORPORATE_ACTION events."""

    bracket_id: str
    cancellation_reason: str
    cancelled_leg_order_ids: tuple[str, ...]


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (EventType.BRACKET_ACTIVATED, BracketActivatedDetail, EventGroup.BRACKET),
    (EventType.BRACKET_COMPLETED, BracketCompletedDetail, EventGroup.BRACKET),
    (EventType.BRACKET_DISSOLVED, BracketDissolvedDetail, EventGroup.BRACKET),
    (EventType.BRACKET_MODIFIED, BracketModifiedDetail, EventGroup.BRACKET),
    (
        EventType.BRACKET_INCOMPLETE_WARNING,
        BracketIncompleteWarningDetail,
        EventGroup.BRACKET,
    ),
    (
        EventType.BRACKET_CANCELLED_CORPORATE_ACTION,
        BracketCancelledCorporateActionDetail,
        EventGroup.BRACKET,
    ),
]


__all__ = [
    "BracketActivatedDetail",
    "BracketCancelledCorporateActionDetail",
    "BracketCompletedDetail",
    "BracketDissolvedDetail",
    "BracketIncompleteWarningDetail",
    "BracketModifiedDetail",
]
