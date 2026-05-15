"""Active risk parameter set aggregate (Tier 3 — raw state category 4d).

Per ``state-persistence.md`` § Tier 3, ``ActiveRiskParameterSet`` is the
regime-multiplied parameter snapshot the OMS exposes to consumers, recomputable
from Tier 1 + Tier 2 data plus the regime adaptation layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from alphamind._kernel.regime import RegimeLabel, RegimeTransitionState
from alphamind.portfolio_state.aggregates.risk_budget import _assert_unique_rule_ids

__all__ = [
    "ActiveRiskParameterEntry",
    "ActiveRiskParameterSet",
]


@dataclass(frozen=True, slots=True)
class ActiveRiskParameterEntry:
    """One rule's slot in the active risk parameter set."""

    rule_id: str
    rule_label: str
    value: float
    unit: str
    regime_multiplier_applied: float
    base_value: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.value):
            msg = f"value must be finite; got {self.value}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ActiveRiskParameterSet:
    """Raw state 4d — active risk parameter set."""

    regime_label: RegimeLabel
    transition_state: RegimeTransitionState
    transition_invocations_remaining: int
    parameter_change_flag: bool
    entries: tuple[ActiveRiskParameterEntry, ...]
    active_overlays: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.transition_invocations_remaining < 0:
            msg = (
                f"transition_invocations_remaining must be >= 0; "
                f"got {self.transition_invocations_remaining}"
            )
            raise ValueError(msg)
        if (
            self.transition_state == RegimeTransitionState.STABLE
            and self.transition_invocations_remaining != 0
        ):
            msg = "transition_invocations_remaining must be 0 when transition_state is STABLE"
            raise ValueError(msg)
        _assert_unique_rule_ids(self.entries)
