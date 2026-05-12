"""Per-rule selector dispatch table (ALP-438).

Maps each immediate-action rule id to the breach-behavior selector primitive
that picks the position to close. The per-selector signatures differ (drawdown
+ margin-call take liquidity; per-position-max-loss takes the breaching
position id; etc.) — the dispatcher reads the table to *resolve* the selector,
then constructs the per-selector keyword arguments at the call site.

A rule id that is not in this table is either:

* A deferred-classification rule (``sector_concentration``,
  ``unrealized_pnl_pct_of_portfolio``, …) — should never reach the
  ``on_immediate_breach`` callback. The dispatcher treats it as a structural
  error.
* An unknown rule id — the dispatcher likewise raises.

``selector_for`` returns ``None`` for both cases; the dispatcher is responsible
for converting that into the structural error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from alphamind.risk_guardrails.breach_behavior import (
    PositionSelectionResult,
    select_for_drawdown_breach,
    select_for_margin_call,
    select_for_position_max_loss,
    select_for_single_short_max_size_breach,
    select_for_total_short_exposure_breach,
)

# Selector signatures differ per breach type — the dispatch table stores
# heterogeneous callables that all return ``PositionSelectionResult``. The
# caller (``CascadeDispatcher``) inspects the rule id and dispatches the
# per-rule keyword arguments before invocation.
type PositionSelector = Callable[..., PositionSelectionResult]


RULE_SELECTOR_DISPATCH: Mapping[str, PositionSelector] = {
    "daily_drawdown": select_for_drawdown_breach,
    "cumulative_drawdown": select_for_drawdown_breach,
    "per_position_max_loss": select_for_position_max_loss,
    "total_short_exposure": select_for_total_short_exposure_breach,
    "single_short_max_size": select_for_single_short_max_size_breach,
    "margin_call": select_for_margin_call,
}


def selector_for(rule_id: str) -> PositionSelector | None:
    """Return the selector for *rule_id*, or ``None`` if no selector is registered.

    ``None`` covers both deferred-classification rules (which should never have
    reached the ``on_immediate_breach`` callback) and unknown rule ids. The
    caller (cascade dispatcher) treats either as a structural error.
    """
    return RULE_SELECTOR_DISPATCH.get(rule_id)


__all__ = [
    "RULE_SELECTOR_DISPATCH",
    "PositionSelector",
    "selector_for",
]
