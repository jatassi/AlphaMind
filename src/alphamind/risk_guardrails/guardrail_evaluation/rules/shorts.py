"""Short-specific rule specs (story 04).

Three rules in this category, all gated on
``feature_flags.short_selling_enabled=True`` via ``requires_shorts=True``:

* ``total_short_pct`` — sum of all short notionals as % of portfolio.
* ``single_short_max_pct`` — max single-short size as % of portfolio (max
  pattern, like ``position_max_size_pct``).
* ``borrow_cost_budget_pct_per_day`` — daily borrow accrual budget.

All three see only ``Direction.SHORT`` proposals; long proposals contribute 0.
Borrow-cost reads ``proposal.daily_borrow_cost_usd`` (set by the caller per
story 01's optional field).
"""

from __future__ import annotations

from alphamind.risk_guardrails.guardrail_evaluation.rules._helpers import (
    RuleSpec,
    existing_position,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    Direction,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
)

# ---------------------------------------------------------------------------
# total_short_pct
# ---------------------------------------------------------------------------


def _total_short_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.total_short_pct


def _total_short_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Magnitude contribution: |signed_notional| for OPEN/ADD; -|existing| for CLOSE."""
    if proposal.direction is not Direction.SHORT:
        return 0.0
    if proposal.action in (Action.OPEN, Action.ADD):
        return abs(dae.signed_notional_usd) / state.portfolio_value_usd * 100.0
    if proposal.action is Action.CLOSE:
        existing = existing_position(proposal, state)
        if existing is None:
            return 0.0
        return -existing.notional_usd / state.portfolio_value_usd * 100.0
    return 0.0


# ---------------------------------------------------------------------------
# single_short_max_pct
# ---------------------------------------------------------------------------


def _single_short_max_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.single_short_max_pct


def _single_short_max_read_breaching_position_id(
    state: PortfolioStateSnapshot, config: LibraryConfig
) -> str | None:
    return state.single_short_max_position_id


def _single_short_max_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Same max-pattern as ``position_max_size_pct``, scoped to shorts."""
    if proposal.direction is not Direction.SHORT:
        return 0.0
    if proposal.action not in (Action.OPEN, Action.ADD):
        return 0.0
    proposal_size_pct = abs(dae.signed_notional_usd) / state.portfolio_value_usd * 100.0
    return max(0.0, proposal_size_pct - state.single_short_max_pct)


# ---------------------------------------------------------------------------
# borrow_cost_budget_pct_per_day
# ---------------------------------------------------------------------------


def _borrow_cost_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.daily_borrow_cost_pct


def _borrow_cost_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Reads ``proposal.daily_borrow_cost_usd`` set by the caller.

    For short-equity OPEN/ADD: positive contribution. For CLOSE: negative
    contribution (closing a short releases its borrow accrual). Long, options,
    and ADJUST/CANCEL proposals contribute 0.
    """
    if proposal.direction is not Direction.SHORT or proposal.asset_type is not AssetType.EQUITY:
        return 0.0
    cost_usd = _borrow_cost_change_usd(proposal, state)
    return cost_usd / state.portfolio_value_usd * 100.0


def _borrow_cost_change_usd(proposal: ProposedDelta, state: PortfolioStateSnapshot) -> float:
    if proposal.action in (Action.OPEN, Action.ADD):
        return proposal.daily_borrow_cost_usd or 0.0
    if proposal.action is Action.CLOSE:
        existing = existing_position(proposal, state)
        if existing is None or existing.daily_borrow_cost_usd is None:
            return 0.0
        return -existing.daily_borrow_cost_usd
    return 0.0


# ---------------------------------------------------------------------------
# Registry entry point
# ---------------------------------------------------------------------------


def shorts_specs() -> tuple[RuleSpec, ...]:
    return (
        RuleSpec(
            rule_id="total_short_pct",
            unit="% of portfolio",
            read_current=_total_short_read_current,
            contribute=_total_short_contribute,
            effective_limit_key="total_short_pct",
            requires_shorts=True,
        ),
        RuleSpec(
            rule_id="single_short_max_pct",
            unit="% of portfolio",
            read_current=_single_short_max_read_current,
            contribute=_single_short_max_contribute,
            effective_limit_key="single_short_max_pct",
            requires_shorts=True,
            read_breaching_position_id=_single_short_max_read_breaching_position_id,
        ),
        RuleSpec(
            rule_id="borrow_cost_budget_pct_per_day",
            unit="% of portfolio per day",
            read_current=_borrow_cost_read_current,
            contribute=_borrow_cost_contribute,
            effective_limit_key="borrow_cost_budget_pct_per_day",
            requires_shorts=True,
        ),
    )
