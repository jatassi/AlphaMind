"""Risk-budget consumption aggregate (Tier 3 — raw state category 4c).

Per ``state-persistence.md`` § Tier 3, ``RiskBudgetConsumption`` is a
pre-computed snapshot summarizing each guardrail rule's current value, limit,
and headroom — recomputable from Tier 1 + Tier 2 data, persisted as an aggregate
to accelerate the read path.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from alphamind._kernel.regime import RiskZone

__all__ = [
    "RiskBudgetConsumption",
    "RiskBudgetEntry",
]


class _HasRuleId(Protocol):
    """Structural type for any object exposing a ``rule_id: str`` attribute.

    Frozen-slotted dataclasses don't satisfy a Protocol whose attribute is
    typed as a mutable ``str``; declaring it via a read-only property keeps
    the structural match working for ``RiskBudgetEntry`` /
    ``ActiveRiskParameterEntry``.
    """

    @property
    def rule_id(self) -> str: ...


def _assert_unique_rule_ids(entries: Iterable[_HasRuleId]) -> None:
    ids = [e.rule_id for e in entries]
    if len(ids) != len(set(ids)):
        msg = "entries must have unique rule_id values"
        raise ValueError(msg)


def _check_finite(value: float, field_name: str) -> None:
    if not math.isfinite(value):
        msg = f"{field_name} must be finite; got {value}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RiskBudgetEntry:
    """One rule's slot in the risk budget consumption snapshot."""

    rule_id: str
    rule_label: str
    current_value: float
    limit_value: float
    headroom: float
    headroom_pct_of_limit: float
    zone: RiskZone
    unit: str
    cumulative_invocation_impact_value: float

    def __post_init__(self) -> None:
        _check_finite(self.current_value, "current_value")
        _check_finite(self.limit_value, "limit_value")
        _check_finite(self.headroom, "headroom")
        _check_finite(self.cumulative_invocation_impact_value, "cumulative_invocation_impact_value")
        if not (0.0 <= self.headroom_pct_of_limit <= 100.0):
            msg = (
                f"headroom_pct_of_limit must satisfy 0 <= value <= 100; "
                f"got {self.headroom_pct_of_limit}"
            )
            raise ValueError(msg)
        expected = self.limit_value - self.current_value
        if not math.isclose(self.headroom, expected, abs_tol=1e-9):
            msg = f"headroom ({self.headroom}) must equal limit_value - current_value ({expected})"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RiskBudgetConsumption:
    """Raw state 4c — risk budget consumption snapshot."""

    entries: tuple[RiskBudgetEntry, ...]

    def __post_init__(self) -> None:
        _assert_unique_rule_ids(self.entries)

    def entries_by_zone(self, zone: RiskZone) -> tuple[RiskBudgetEntry, ...]:
        """Return all entries whose zone matches the argument."""
        return tuple(e for e in self.entries if e.zone == zone)

    def breaching_entries(self) -> tuple[RiskBudgetEntry, ...]:
        """Return entries with zone in {CRITICAL, BLOCKED}."""
        return tuple(e for e in self.entries if e.zone in (RiskZone.CRITICAL, RiskZone.BLOCKED))

    def entry_by_rule_id(self, rule_id: str) -> RiskBudgetEntry | None:
        """Return the entry matching rule_id, or None."""
        for entry in self.entries:
            if entry.rule_id == rule_id:
                return entry
        return None
