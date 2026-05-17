"""Per-rule kwargs providers for the cascade dispatcher.

Each entry in :func:`build_per_rule_kwargs_providers` is a
:data:`PerRuleKwargsProvider` keyed by the ``rule_id`` of an
``immediate_engine`` rule from ``config/guardrails.yaml``. The provider
extracts the selector's keyword arguments from the per-tick
:class:`BreachDispatchContext` and the firing :class:`RuleEvaluation`, and
the dispatcher invokes ``selector_for(rule_id)(**kwargs)`` to obtain the
position-selection result.

Per-position rules (``position_max_loss_*_pct``, ``single_short_max_pct``)
read ``rule.breaching_position_id`` directly; the library projection
populates it upstream so the providers do not re-scan ``open_positions``.

Sign convention: ``rule.limit_value`` and ``rule.current_value`` for max-loss
rules are positive-magnitude percentages (e.g., ``3.0`` for a 3% max loss).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphamind.execution.continuous_monitor.breach_loop.result import RuleEvaluation
from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    BreachDispatchContext,
    PerRuleKwargsProvider,
)
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
        "position_max_loss_equity_pct": _position_max_loss_provider,
        "position_max_loss_options_pct": _position_max_loss_provider,
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
# open_positions, loss_pct, limit_pct). Equity and options share the
# provider; the upstream rule classifier decides which rule_id fired, and
# the selector closes the single position the rule names.
# ---------------------------------------------------------------------------


def _position_max_loss_provider(
    rule: RuleEvaluation, context: BreachDispatchContext
) -> dict[str, Any]:
    return {
        "breaching_position_id": _require_breaching_position_id(rule),
        "open_positions": context.open_positions,
        "loss_pct": rule.current_value,
        "limit_pct": rule.limit_value,
    }


# ---------------------------------------------------------------------------
# Single-short max size — selector signature is (breaching_position_id,
# open_positions, single_short_max_pct_of_portfolio, config).
# ---------------------------------------------------------------------------


def _make_single_short_max_provider(
    breach_behavior_config: BreachBehaviorConfig,
) -> PerRuleKwargsProvider:
    def _provider(rule: RuleEvaluation, context: BreachDispatchContext) -> dict[str, Any]:
        return {
            "breaching_position_id": _require_breaching_position_id(rule),
            "open_positions": context.open_positions,
            "single_short_max_pct_of_portfolio": rule.limit_value,
            "config": breach_behavior_config,
        }

    return _provider


def _require_breaching_position_id(rule: RuleEvaluation) -> str:
    """Per-position rules must carry the breaching id from the library projection.

    ``None`` indicates the rule classifier surfaced an immediate breach
    without identifying a breaching position — a structural error.
    """
    if rule.breaching_position_id is None:
        msg = (
            f"{rule.rule_id} fired without a breaching_position_id on the rule "
            "evaluation; structural error — the library projection is expected "
            "to populate this for per-position rules"
        )
        raise ValueError(msg)
    return rule.breaching_position_id


__all__ = ["build_per_rule_kwargs_providers"]
