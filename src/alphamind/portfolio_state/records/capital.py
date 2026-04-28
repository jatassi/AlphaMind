"""Capital and capacity state records (raw state categories 2c, 4a, 4c, 4d)."""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _HasRuleId(Protocol):
    rule_id: str


def _assert_unique_rule_ids(entries: tuple[_HasRuleId, ...]) -> None:
    ids = [e.rule_id for e in entries]
    if len(ids) != len(set(ids)):
        msg = "entries must have unique rule_id values"
        raise ValueError(msg)


class RegimeLabel(StrEnum):
    """Volatility regime classification."""

    LOW_VOL = "LOW_VOL"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    CRISIS = "CRISIS"


class RegimeTransitionState(StrEnum):
    """Whether the system is stable or transitioning between volatility regimes."""

    STABLE = "STABLE"
    TIGHTENING = "TIGHTENING"
    LOOSENING = "LOOSENING"


class RiskZone(StrEnum):
    """Proximity zone for a risk rule limit."""

    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    BLOCKED = "BLOCKED"


class DrawdownTier(StrEnum):
    """Cumulative drawdown progressive response tier."""

    CONSTRAINED = "CONSTRAINED"
    HEAVILY_CONSTRAINED = "HEAVILY_CONSTRAINED"
    FULL_HALT = "FULL_HALT"


# ---------------------------------------------------------------------------
# Annotated type alias: finite float field
# ---------------------------------------------------------------------------

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class UnsettledProceedsEntry(BaseModel):
    """A single in-flight settlement."""

    model_config = ConfigDict(frozen=True)

    settlement_date: datetime
    amount_usd: _FiniteFloat
    source_transaction_id: str

    @field_validator("settlement_date")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "settlement_date must be timezone-aware"
            raise ValueError(msg)
        return v


class CashLedger(BaseModel):
    """Raw state 4a — cash and buying power."""

    model_config = ConfigDict(frozen=True)

    current_cash_usd: _FiniteFloat
    settled_cash_usd: _FiniteFloat
    reserved_capital_usd: _FiniteFloat
    available_buying_power_usd: _FiniteFloat
    margin_held_usd: _FiniteFloat
    unsettled_proceeds: tuple[UnsettledProceedsEntry, ...]
    cash_pct_of_portfolio: Annotated[float, Field(ge=0.0, le=100.0)]
    true_deployable_capital_usd: _FiniteFloat
    regt_excess_trailing_30d_usd: _FiniteFloat
    regt_excess_trailing_90d_usd: _FiniteFloat
    regt_excess_lifetime_usd: _FiniteFloat


class DrawdownState(BaseModel):
    """Raw state 2c — drawdown tracking."""

    model_config = ConfigDict(frozen=True)

    current_drawdown_pct: Annotated[float, Field(ge=0.0)]
    equity_high_water_mark_usd: _FiniteFloat
    drawdown_duration_hours: Annotated[float, Field(ge=0.0)]
    lifetime_max_drawdown_pct: Annotated[float, Field(ge=0.0)]
    intraday_drawdown_pct: Annotated[float, Field(ge=0.0)]
    daily_zone: RiskZone
    cumulative_zone: RiskZone
    cumulative_tier: DrawdownTier | None
    drawdown_by_source_pct: dict[str, float]


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


class ActiveRiskParameterEntry(BaseModel):
    """One rule's slot in the active risk parameter set."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_label: str
    value: _FiniteFloat
    unit: str
    regime_multiplier_applied: float
    base_value: float


class ActiveRiskParameterSet(BaseModel):
    """Raw state 4d — active risk parameter set."""

    model_config = ConfigDict(frozen=True)

    regime_label: RegimeLabel
    transition_state: RegimeTransitionState
    transition_invocations_remaining: Annotated[int, Field(ge=0)]
    parameter_change_flag: bool
    entries: tuple[ActiveRiskParameterEntry, ...]
    active_overlays: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_stable_invocations(self) -> ActiveRiskParameterSet:
        if (
            self.transition_state == RegimeTransitionState.STABLE
            and self.transition_invocations_remaining != 0
        ):
            msg = "transition_invocations_remaining must be 0 when transition_state is STABLE"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_unique_entry_rule_ids(self) -> ActiveRiskParameterSet:
        _assert_unique_rule_ids(self.entries)
        return self
