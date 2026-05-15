"""Per-invocation thesis-health view — story ALP-351.

Separates entry-time component state (immutable on
:class:`alphamind.portfolio_state.records.theses.ThesisComponent`) from
per-invocation re-assessment (mutable across invocations, snapshotted here).

The strategist re-assesses each cited supporting signal's status (PRESENT /
STRENGTHENED / WEAKENED / REVERSED — per ``portfolio-state.md`` § 3b) and the
five-state thesis health classification (ON_TRACK / PARTIALLY_REALIZED /
AT_RISK / STALE / INVALIDATED — per ``thesis-model.md``) at every invocation.
This module captures that re-assessment as an in-memory snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alphamind.portfolio_state.records.theses import (
    SupportingSignal,
    ThesisStatus,
)

__all__ = [
    "ComponentHealthEntry",
    "ThesisHealthSnapshot",
]


@dataclass(frozen=True, slots=True)
class ComponentHealthEntry:
    """Per-component re-assessment from one strategist invocation.

    The strategist produces one entry per active thesis component at each
    invocation, capturing the current state of cited signals.
    """

    component_id: str
    supporting_signals: tuple[SupportingSignal, ...]

    def __post_init__(self) -> None:
        if len(self.component_id) < 1:
            msg = "component_id must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ThesisHealthSnapshot:
    """Per-invocation thesis-health re-assessment from the strategist.

    Produced by the strategist at each invocation; consumed by the PM and
    other downstream agents. Separates entry-time component state (immutable
    on :class:`ThesisComponent`) from per-invocation re-assessment (mutable
    across invocations, snapshotted here).
    """

    thesis_id: str
    invocation_id: str
    snapshot_timestamp: datetime
    health_status: ThesisStatus
    prior_health_status: ThesisStatus | None
    component_health: tuple[ComponentHealthEntry, ...]

    def __post_init__(self) -> None:
        if len(self.thesis_id) < 1:
            msg = "thesis_id must be non-empty"
            raise ValueError(msg)
        if len(self.invocation_id) < 1:
            msg = "invocation_id must be non-empty"
            raise ValueError(msg)

    def health_for_component(self, component_id: str) -> ComponentHealthEntry | None:
        """Return the per-component health entry for ``component_id``, or None."""
        for entry in self.component_health:
            if entry.component_id == component_id:
                return entry
        return None
