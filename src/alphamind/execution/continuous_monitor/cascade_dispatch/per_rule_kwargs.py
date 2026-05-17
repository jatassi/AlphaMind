"""Per-rule kwargs providers for the cascade dispatcher.

Each entry in :func:`build_per_rule_kwargs_providers` is a
:data:`PerRuleKwargsProvider` keyed by the ``rule_id`` of an
``immediate_engine`` rule from ``config/guardrails.yaml``. The provider
extracts the selector's keyword arguments from the per-tick
:class:`BreachDispatchContext` and the firing :class:`RuleEvaluation`, and
the dispatcher invokes ``selector_for(rule_id)(**kwargs)`` to obtain the
position-selection result.

Per-position rules (``position_max_loss_*_pct``, ``single_short_max_pct``)
need a ``breaching_position_id`` that is not currently carried on
:class:`RuleEvaluation`. The providers derive it by re-scanning open
positions — the worst in-class loser for the max-loss rules, the largest
short over the cap for ``single_short_max_pct``. The dispatcher may close a
different position than the one whose breach triggered the rule when ties
or near-ties exist; surfacing the breaching id on the rule evaluation
upstream would let these providers trust the rule instead of re-scanning.

Sign convention: ``rule.limit_value`` and ``rule.current_value`` for max-loss
rules are positive-magnitude percentages (e.g., ``3.0`` for a 3% max loss).
``PositionView.unrealized_pnl_pct`` is signed (negative for a loss), so the
filter negates the limit before comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphamind.execution.continuous_monitor.breach_loop.result import RuleEvaluation
from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    BreachDispatchContext,
    PerRuleKwargsProvider,
)
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import BreachBehaviorConfig


def build_per_rule_kwargs_providers(
    *, breach_behavior_config: BreachBehaviorConfig
) -> Mapping[str, PerRuleKwargsProvider]:
    """Build the ``rule_id → PerRuleKwargsProvider`` map the dispatcher consumes.

    Keys mirror the ``immediate_engine`` rule ids declared in
    ``config/guardrails.yaml``. ``breach_behavior_config`` flows into the
    ``single_short_max_pct`` provider's selector kwargs (the 95% trim factor
    lives there).
    """
    return {
        "daily_drawdown_pct": _drawdown_kwargs_provider,
        "cumulative_drawdown_pct": _drawdown_kwargs_provider,
        "position_max_loss_equity_pct": _make_position_max_loss_provider(InstrumentType.EQUITY),
        "position_max_loss_options_pct": _make_position_max_loss_provider(InstrumentType.OPTIONS),
        "single_short_max_pct": _make_single_short_max_provider(breach_behavior_config),
    }


# ---------------------------------------------------------------------------
# Drawdown — selector signature is (open_positions, liquidity). The rule
# carries a portfolio-level metric, not a position identifier; the selector
# re-derives the worst loser internally.
# ---------------------------------------------------------------------------


def _drawdown_kwargs_provider(
    _rule: RuleEvaluation, context: BreachDispatchContext
) -> dict[str, Any]:
    return {
        "open_positions": context.open_positions,
        "liquidity": context.liquidity,
    }


# ---------------------------------------------------------------------------
# Per-position max loss — selector signature is (breaching_position_id,
# open_positions, loss_pct, limit_pct).
# ---------------------------------------------------------------------------


def _make_position_max_loss_provider(
    instrument_type: InstrumentType,
) -> PerRuleKwargsProvider:
    def _provider(rule: RuleEvaluation, context: BreachDispatchContext) -> dict[str, Any]:
        max_loss_pct = rule.limit_value
        breaching = _select_worst_loss_position(
            context.open_positions,
            instrument_type=instrument_type,
            max_loss_pct=max_loss_pct,
            rule_id=rule.rule_id,
        )
        return {
            "breaching_position_id": breaching.position_id,
            "open_positions": context.open_positions,
            "loss_pct": rule.current_value,
            "limit_pct": max_loss_pct,
        }

    return _provider


def _select_worst_loss_position(
    positions: tuple[PositionView, ...],
    *,
    instrument_type: InstrumentType,
    max_loss_pct: float,
    rule_id: str,
) -> PositionView:
    """Return the in-class position whose loss exceeds ``max_loss_pct``.

    ``max_loss_pct`` is the positive-magnitude threshold (e.g., ``3.0`` for a
    3% max loss). ``unrealized_pnl_pct`` is signed, so the filter compares
    against the negated threshold.
    """
    candidates = [
        p
        for p in positions
        if p.instrument_type is instrument_type and p.unrealized_pnl_pct < -max_loss_pct
    ]
    if not candidates:
        msg = (
            f"{rule_id} fired with no {instrument_type.value} position below "
            f"limit -{max_loss_pct:.2f}%; structural error — the rule classifier "
            f"surfaced an immediate breach with no breaching position"
        )
        raise ValueError(msg)
    return min(candidates, key=lambda p: p.unrealized_pnl_pct)


# ---------------------------------------------------------------------------
# Single-short max size — selector signature is (breaching_position_id,
# open_positions, single_short_max_pct_of_portfolio, config).
# ---------------------------------------------------------------------------


def _make_single_short_max_provider(
    breach_behavior_config: BreachBehaviorConfig,
) -> PerRuleKwargsProvider:
    def _provider(rule: RuleEvaluation, context: BreachDispatchContext) -> dict[str, Any]:
        per_position_cap_pct = rule.limit_value
        breaching = _select_largest_short_over_cap(
            context.open_positions,
            per_position_cap_pct=per_position_cap_pct,
            rule_id=rule.rule_id,
        )
        return {
            "breaching_position_id": breaching.position_id,
            "open_positions": context.open_positions,
            "single_short_max_pct_of_portfolio": per_position_cap_pct,
            "config": breach_behavior_config,
        }

    return _provider


def _select_largest_short_over_cap(
    positions: tuple[PositionView, ...],
    *,
    per_position_cap_pct: float,
    rule_id: str,
) -> PositionView:
    shorts_over = [
        p
        for p in positions
        if p.direction is Direction.SHORT and abs(p.position_weight_pct) > per_position_cap_pct
    ]
    if not shorts_over:
        msg = (
            f"{rule_id} fired with no short position above cap "
            f"{per_position_cap_pct:.2f}%; structural error — the rule classifier "
            f"surfaced an immediate breach with no breaching short"
        )
        raise ValueError(msg)
    return max(shorts_over, key=lambda p: abs(p.position_weight_pct))


__all__ = ["build_per_rule_kwargs_providers"]
