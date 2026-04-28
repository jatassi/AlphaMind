"""Delivery-time field computations for capital, orders, and parameters (story 05d)."""

from __future__ import annotations

from datetime import datetime

from alphamind.portfolio_state.records.capital import ActiveRiskParameterSet, CashLedger


def compute_cash_pct_of_portfolio(
    current_cash_usd: float,
    total_portfolio_value_usd: float,
) -> float:
    """Return cash as a percentage of total portfolio value.

    Returns 0.0 when total_portfolio_value_usd is zero.
    Raises ValueError for any negative input.
    """
    if current_cash_usd < 0:
        msg = f"current_cash_usd must be >= 0; got {current_cash_usd}"
        raise ValueError(msg)
    if total_portfolio_value_usd < 0:
        msg = f"total_portfolio_value_usd must be >= 0; got {total_portfolio_value_usd}"
        raise ValueError(msg)
    if total_portfolio_value_usd == 0:
        return 0.0
    return (current_cash_usd / total_portfolio_value_usd) * 100.0


def compute_true_deployable_capital_usd(cash_ledger: CashLedger) -> float:
    """Return settled cash minus reserved capital and margin held.

    May return a negative value during settlement-cycle compression.
    """
    return (
        cash_ledger.settled_cash_usd
        - cash_ledger.reserved_capital_usd
        - cash_ledger.margin_held_usd
    )


def compute_order_age_hours(submission_timestamp: datetime, now: datetime) -> float:
    """Return order age in fractional hours.

    Both arguments must be timezone-aware. Raises ValueError on mixed tz-awareness.
    May return negative when now < submission_timestamp.
    """
    sub_aware = (
        submission_timestamp.tzinfo is not None
        and submission_timestamp.utcoffset() is not None
    )
    now_aware = now.tzinfo is not None and now.utcoffset() is not None
    if sub_aware != now_aware:
        msg = "submission_timestamp and now must both be timezone-aware or both timezone-naive"
        raise ValueError(msg)
    return (now - submission_timestamp).total_seconds() / 3600.0


def compute_parameter_change_flag(
    current: ActiveRiskParameterSet,
    prior: ActiveRiskParameterSet | None,
) -> bool:
    """Return True when operative risk parameters changed since the prior invocation.

    Returns False when prior is None (first invocation). Compares regime_label,
    (rule_id, value) pairs, and active_overlays; ignores metadata fields.
    """
    if prior is None:
        return False
    if current.regime_label != prior.regime_label:
        return True
    current_pairs = {(e.rule_id, e.value) for e in current.entries}
    prior_pairs = {(e.rule_id, e.value) for e in prior.entries}
    if current_pairs != prior_pairs:
        return True
    return set(current.active_overlays) != set(prior.active_overlays)
