"""Per-rule kwargs providers for the cascade dispatcher (ALP-509).

Each entry in :func:`build_per_rule_kwargs_providers` is a
:data:`PerRuleKwargsProvider` keyed by the ``rule_id`` of an
``immediate_engine`` rule from ``config/guardrails.yaml``. The provider
extracts the selector's keyword arguments from the per-tick
:class:`BreachDispatchContext` and the firing :class:`RuleEvaluation`, and
the dispatcher invokes ``selector_for(rule_id)(**kwargs)`` to obtain the
position-selection result.

Per-position rules (``position_max_loss_*_pct``, ``single_short_max_pct``)
need a ``breaching_position_id`` that is not currently carried on
:class:`RuleEvaluation`. The providers derive it from the open-position
snapshot — the worst in-class loss for the max-loss rules, the largest short
over the cap for ``single_short_max_pct``. A future enhancement that surfaces
the breaching id directly on the rule evaluation would let these providers
trust the rule instead of re-scanning.
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
# Drawdown — selector signature is (open_positions, liquidity).
# ---------------------------------------------------------------------------


def _drawdown_kwargs_provider(
    rule: RuleEvaluation, context: BreachDispatchContext
) -> dict[str, Any]:
    """Provider for daily / cumulative drawdown rules.

    ``select_for_drawdown_breach`` picks the position with the largest
    unrealized loss; ``rule`` carries the drawdown metric, not a position
    identifier, so the selector receives the full open-position set and
    re-derives the worst.
    """
    del rule
    return {
        "open_positions": context.open_positions,
        "liquidity": context.liquidity,
    }


# ---------------------------------------------------------------------------
# Per-position max loss — selector signature is (breaching_position_id,
# open_positions, loss_pct, limit_pct). The breaching position is the worst
# in-class position whose unrealized_pnl_pct is below the rule's limit.
# ---------------------------------------------------------------------------


def _make_position_max_loss_provider(
    instrument_type: InstrumentType,
) -> PerRuleKwargsProvider:
    def _provider(rule: RuleEvaluation, context: BreachDispatchContext) -> dict[str, Any]:
        breaching = _select_worst_loss_position(
            context.open_positions,
            instrument_type=instrument_type,
            limit_pct=rule.limit_value,
            rule_id=rule.rule_id,
        )
        return {
            "breaching_position_id": breaching.position_id,
            "open_positions": context.open_positions,
            "loss_pct": rule.current_value,
            "limit_pct": rule.limit_value,
        }

    return _provider


def _select_worst_loss_position(
    positions: tuple[PositionView, ...],
    *,
    instrument_type: InstrumentType,
    limit_pct: float,
    rule_id: str,
) -> PositionView:
    """Return the in-class position with the largest loss below ``limit_pct``.

    ``limit_pct`` is signed (negative, e.g., ``-3.0``); a position is breaching
    when its ``unrealized_pnl_pct`` is strictly less (more negative).
    """
    candidates = [
        p
        for p in positions
        if p.instrument_type is instrument_type and p.unrealized_pnl_pct < limit_pct
    ]
    if not candidates:
        msg = (
            f"{rule_id} fired with no {instrument_type.value} position below "
            f"limit_pct={limit_pct:.2f}%; structural error — the rule classifier "
            f"surfaced an immediate breach with no breaching position"
        )
        raise ValueError(msg)
    return min(candidates, key=lambda p: p.unrealized_pnl_pct)


# ---------------------------------------------------------------------------
# Single-short max size — selector signature is (breaching_position_id,
# open_positions, single_short_max_pct_of_portfolio, config). The breaching
# short is the largest short whose absolute weight exceeds the per-position
# cap (``rule.limit_value``).
# ---------------------------------------------------------------------------


def _make_single_short_max_provider(
    breach_behavior_config: BreachBehaviorConfig,
) -> PerRuleKwargsProvider:
    def _provider(rule: RuleEvaluation, context: BreachDispatchContext) -> dict[str, Any]:
        breaching = _select_largest_short_over_cap(
            context.open_positions,
            cap_pct=rule.limit_value,
            rule_id=rule.rule_id,
        )
        return {
            "breaching_position_id": breaching.position_id,
            "open_positions": context.open_positions,
            "single_short_max_pct_of_portfolio": rule.limit_value,
            "config": breach_behavior_config,
        }

    return _provider


def _select_largest_short_over_cap(
    positions: tuple[PositionView, ...],
    *,
    cap_pct: float,
    rule_id: str,
) -> PositionView:
    shorts_over = [
        p
        for p in positions
        if p.direction is Direction.SHORT and abs(p.position_weight_pct) > cap_pct
    ]
    if not shorts_over:
        msg = (
            f"{rule_id} fired with no short position above cap "
            f"{cap_pct:.2f}%; structural error — the rule classifier "
            f"surfaced an immediate breach with no breaching short"
        )
        raise ValueError(msg)
    return max(shorts_over, key=lambda p: abs(p.position_weight_pct))


__all__ = ["build_per_rule_kwargs_providers"]
