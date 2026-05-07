"""Per-invocation view records — mutable across invocations, not persisted on Tier 1.

Hosts ephemeral, snapshotted projections produced by an agent each invocation.
Distinct from ``records/`` (immutable Tier 1 entities), ``events/`` (lifecycle
records), and ``aggregates/`` (derived rollups).
"""

from __future__ import annotations

from alphamind.portfolio_state.views.thesis_health import (
    ComponentHealthEntry,
    ThesisHealthSnapshot,
)

__all__ = [
    "ComponentHealthEntry",
    "ThesisHealthSnapshot",
]
