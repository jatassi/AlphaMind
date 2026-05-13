"""Thesis event details — thesis creation, component changes, resolution."""

from __future__ import annotations

from dataclasses import dataclass

from alphamind.portfolio_state.events.types import EventGroup, EventType


@dataclass(frozen=True, slots=True)
class ThesisCreatedDetail:
    """Detail payload for THESIS_CREATED events."""

    thesis_id: str
    summary: str


@dataclass(frozen=True, slots=True)
class ThesisComponentAddedDetail:
    """Detail payload for THESIS_COMPONENT_ADDED events."""

    component_id: str
    component_type: str


@dataclass(frozen=True, slots=True)
class ThesisComponentUpdatedDetail:
    """Detail payload for THESIS_COMPONENT_UPDATED events."""

    component_id: str
    field_changed: str
    old_value: str
    new_value: str


@dataclass(frozen=True, slots=True)
class ThesisResolvedDetail:
    """Detail payload for THESIS_RESOLVED events."""

    resolution_category: str
    component_outcomes_json: dict[str, str]


@dataclass(frozen=True, slots=True)
class ThesisStatusChangedDetail:
    """Detail payload for THESIS_STATUS_CHANGED events."""

    old_status: str
    new_status: str


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (EventType.THESIS_CREATED, ThesisCreatedDetail, EventGroup.THESIS),
    (EventType.THESIS_COMPONENT_ADDED, ThesisComponentAddedDetail, EventGroup.THESIS),
    (
        EventType.THESIS_COMPONENT_UPDATED,
        ThesisComponentUpdatedDetail,
        EventGroup.THESIS,
    ),
    (EventType.THESIS_RESOLVED, ThesisResolvedDetail, EventGroup.THESIS),
    (EventType.THESIS_STATUS_CHANGED, ThesisStatusChangedDetail, EventGroup.THESIS),
]


__all__ = [
    "ThesisComponentAddedDetail",
    "ThesisComponentUpdatedDetail",
    "ThesisCreatedDetail",
    "ThesisResolvedDetail",
    "ThesisStatusChangedDetail",
]
