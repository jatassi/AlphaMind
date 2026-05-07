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

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from alphamind.portfolio_state.records.theses import (
    SupportingSignal,
    ThesisStatus,
)

__all__ = [
    "ComponentHealthEntry",
    "ThesisHealthSnapshot",
]


class ComponentHealthEntry(BaseModel):
    """Per-component re-assessment from one strategist invocation.

    The strategist produces one entry per active thesis component at each
    invocation, capturing the current state of cited signals.
    """

    model_config = ConfigDict(frozen=True)

    component_id: str = Field(min_length=1)
    supporting_signals: tuple[SupportingSignal, ...]


class ThesisHealthSnapshot(BaseModel):
    """Per-invocation thesis-health re-assessment from the strategist.

    Produced by the strategist at each invocation; consumed by the PM and
    other downstream agents. Separates entry-time component state (immutable
    on :class:`ThesisComponent`) from per-invocation re-assessment (mutable
    across invocations, snapshotted here).
    """

    model_config = ConfigDict(frozen=True)

    thesis_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    snapshot_timestamp: datetime
    health_status: ThesisStatus
    prior_health_status: ThesisStatus | None
    component_health: tuple[ComponentHealthEntry, ...]

    def health_for_component(self, component_id: str) -> ComponentHealthEntry | None:
        """Return the per-component health entry for ``component_id``, or None."""
        for entry in self.component_health:
            if entry.component_id == component_id:
                return entry
        return None
