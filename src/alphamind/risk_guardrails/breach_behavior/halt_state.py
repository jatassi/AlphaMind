"""Halt-state computation primitive (story 05a).

Derives a ``HaltState`` from the current ``DrawdownState`` and the active risk
parameter set. Halt mode is active when daily drawdown reaches 100% of the
daily limit OR cumulative drawdown reaches the tier-3 ``FULL_HALT`` threshold
(both can be simultaneously active). When neither condition holds the function
returns ``None``; the rendering layer's halt-mode wrappers are not invoked.

Design reference: ``docs/design/06-risk-guardrails/breach-behavior.md`` §
*Drawdown halt mode* and ``docs/design/06-risk-guardrails/state-delivery.md`` §
*Halt-mode header modifications*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier, HaltState

if TYPE_CHECKING:
    # ``ActiveRiskParameterSet``/``DrawdownState`` live in
    # ``portfolio_state.records.capital``, which re-exports the four
    # risk-guardrail enums from this package. Eager import would cycle.
    # Annotation-only usage is sound under ``from __future__ import
    # annotations``.
    from alphamind.portfolio_state.records.capital import (
        ActiveRiskParameterSet,
        DrawdownState,
    )

_DAILY_DRAWDOWN_RULE_ID = "daily_drawdown_pct"


def compute_halt_state(
    *,
    drawdown_state: DrawdownState,
    active_risk_parameters: ActiveRiskParameterSet,
) -> HaltState | None:
    """Determine whether halt mode is active and produce a ``HaltState`` if so.

    Halt mode is active when EITHER:

    1. Daily drawdown reaches 100% of the daily-drawdown limit, i.e.,
       ``drawdown_state.intraday_drawdown_pct >= daily_drawdown_limit_pct``.
    2. Cumulative drawdown reached tier 3 full halt, i.e.,
       ``drawdown_state.cumulative_tier == DrawdownTier.FULL_HALT``.

    Both can be active simultaneously (e.g., a deep selloff that crosses both
    daily and cumulative full-halt thresholds in the same session). The
    returned ``HaltState`` reflects both flags. Tier 1 (``CONSTRAINED``) and
    tier 2 (``HEAVILY_CONSTRAINED``) cumulative responses apply progressive
    parameter overrides via story 04b's ``apply_progressive_tier_overrides``
    and are *not* halts; only tier 3 maps to halt mode.

    The primitive is stateless and reflects current drawdown conditions only.
    Session-latching ("once halt fires, stays for the rest of the session") is
    layered atop this primitive by the continuous monitor; recovery semantics
    follow the same pattern as the cumulative tier classifier — no hysteresis
    here, recovery handled by the caller.

    Args:
        drawdown_state: The current drawdown state from the portfolio-state
            snapshot. ``intraday_drawdown_pct`` is the intra-day drawdown from
            opening equity; ``cumulative_tier`` carries story 04b's classifier
            output.
        active_risk_parameters: The current active parameter set; must contain
            a ``daily_drawdown_pct`` rule entry (the regime-resolved daily
            limit).

    Returns:
        A frozen ``HaltState`` if at least one halt condition is active;
        ``None`` otherwise.

    Raises:
        ValueError: when ``active_risk_parameters`` lacks the
            ``daily_drawdown_pct`` entry — every regime resolves a daily
            drawdown limit, so absence indicates a structural error.
    """
    daily_limit_pct = _resolve_daily_drawdown_limit(active_risk_parameters)

    daily_halt_active = drawdown_state.intraday_drawdown_pct >= daily_limit_pct
    cumulative_full_halt_active = drawdown_state.cumulative_tier == DrawdownTier.FULL_HALT

    if not (daily_halt_active or cumulative_full_halt_active):
        return None

    return HaltState(
        daily_halt_active=daily_halt_active,
        cumulative_full_halt_active=cumulative_full_halt_active,
        daily_drawdown_pct=drawdown_state.intraday_drawdown_pct,
        daily_drawdown_limit_pct=daily_limit_pct,
    )


def _resolve_daily_drawdown_limit(active_risk_parameters: ActiveRiskParameterSet) -> float:
    """Look up the regime-resolved daily-drawdown limit from the parameter set.

    Raises ``ValueError`` if no entry with rule_id ``daily_drawdown_pct``
    exists — the resolver is contractually required to populate it for every
    regime.
    """
    for entry in active_risk_parameters.entries:
        if entry.rule_id == _DAILY_DRAWDOWN_RULE_ID:
            return entry.value
    msg = (
        f"active_risk_parameters missing required entry {_DAILY_DRAWDOWN_RULE_ID!r}; "
        "every regime must resolve a daily-drawdown limit"
    )
    raise ValueError(msg)
