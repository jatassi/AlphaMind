"""Capital rule specs (story 04).

Two rules in this category:

* ``min_cash_reserve_pct`` — minimum cash reserve as % of portfolio. The only
  inverse rule (``inverse=True`` on the spec): the limit is a floor, so the
  projection engine flips classification.
* ``pending_order_capital_pct`` — capital reserved for unfilled non-marketable
  limit orders.

Capital impact contributions:

* Long equity OPEN/ADD reduces cash by ``proposal.notional_usd``.
* Option OPEN/ADD reduces cash by the premium-at-risk
  (``proposal.notional_usd``).
* CLOSE releases cash equal to the existing position's notional (long: trade
  proceeds credited; short: broker re-credits the proceeds held aside).
* Short equity OPEN does not move cash; Reg T margin is tracked separately
  upstream.
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
# min_cash_reserve_pct
# ---------------------------------------------------------------------------


def _min_cash_reserve_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.cash_usd / state.portfolio_value_usd * 100.0


def _min_cash_reserve_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Capital impact in % of portfolio (signed: cash decreases for OPEN/ADD)."""
    return _cash_change_usd(proposal, state) / state.portfolio_value_usd * 100.0


def _cash_change_usd(proposal: ProposedDelta, state: PortfolioStateSnapshot) -> float:
    if proposal.action in (Action.OPEN, Action.ADD):
        if proposal.direction is Direction.SHORT and proposal.asset_type is AssetType.EQUITY:
            return 0.0
        return -proposal.notional_usd
    if proposal.action is Action.CLOSE:
        existing = existing_position(proposal, state)
        if existing is None:
            return 0.0
        return existing.notional_usd
    return 0.0


# ---------------------------------------------------------------------------
# pending_order_capital_pct
# ---------------------------------------------------------------------------


def _pending_order_capital_read_current(
    state: PortfolioStateSnapshot, config: LibraryConfig
) -> float:
    return state.reserved_for_pending_orders_usd / state.portfolio_value_usd * 100.0


def _pending_order_capital_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Non-marketable limit OPENs reserve capital; CANCEL releases it.

    The proposal carries ``reserves_capital`` to mark non-marketable limits.
    Marketable orders, options (premium paid up front), and CLOSE/ADJUST
    proposals reserve nothing.
    """
    if proposal.action is Action.OPEN and proposal.reserves_capital:
        return proposal.notional_usd / state.portfolio_value_usd * 100.0
    if proposal.action is Action.CANCEL:
        existing = existing_position(proposal, state)
        if existing is None:
            return 0.0
        return -existing.reserves_capital_usd / state.portfolio_value_usd * 100.0
    return 0.0


# ---------------------------------------------------------------------------
# Registry entry point
# ---------------------------------------------------------------------------


def capital_specs() -> tuple[RuleSpec, ...]:
    return (
        RuleSpec(
            rule_id="min_cash_reserve_pct",
            unit="% of portfolio",
            read_current=_min_cash_reserve_read_current,
            contribute=_min_cash_reserve_contribute,
            effective_limit_key="min_cash_reserve_pct",
            inverse=True,
        ),
        RuleSpec(
            rule_id="pending_order_capital_pct",
            unit="% of portfolio",
            read_current=_pending_order_capital_read_current,
            contribute=_pending_order_capital_contribute,
            effective_limit_key="pending_order_capital_pct",
        ),
    )
