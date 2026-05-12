"""Risk-budget consumption aggregate (Tier 3 — raw state category 4c).

Per ``state-persistence.md`` § Tier 3, ``RiskBudgetConsumption`` is a
pre-computed snapshot summarizing each guardrail rule's current value, limit,
and headroom — recomputable from Tier 1 + Tier 2 data, persisted as an aggregate
to accelerate the read path.
"""

from __future__ import annotations

import math
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alphamind._kernel.regime import RiskZone

__all__ = [
    "RiskBudgetConsumption",
    "RiskBudgetEntry",
]

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class _HasRuleId(Protocol):
    rule_id: str


def _assert_unique_rule_ids(entries: tuple[_HasRuleId, ...]) -> None:
    ids = [e.rule_id for e in entries]
    if len(ids) != len(set(ids)):
        msg = "entries must have unique rule_id values"
        raise ValueError(msg)


class RiskBudgetEntry(BaseModel):
    """One rule's slot in the risk budget consumption snapshot."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_label: str
    current_value: _FiniteFloat
    limit_value: _FiniteFloat
    headroom: _FiniteFloat
    headroom_pct_of_limit: Annotated[float, Field(ge=0.0, le=100.0)]
    zone: RiskZone
    unit: str
    cumulative_invocation_impact_value: _FiniteFloat

    @model_validator(mode="after")
    def _validate_headroom_identity(self) -> RiskBudgetEntry:
        expected = self.limit_value - self.current_value
        if not math.isclose(self.headroom, expected, abs_tol=1e-9):
            msg = f"headroom ({self.headroom}) must equal limit_value - current_value ({expected})"
            raise ValueError(msg)
        return self


class RiskBudgetConsumption(BaseModel):
    """Raw state 4c — risk budget consumption snapshot."""

    model_config = ConfigDict(frozen=True)

    entries: tuple[RiskBudgetEntry, ...]

    @model_validator(mode="after")
    def _validate_unique_rule_ids(self) -> RiskBudgetConsumption:
        _assert_unique_rule_ids(self.entries)
        return self

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
