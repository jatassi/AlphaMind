"""Exposure rule specs (story 04).

Five rules in this category:

* ``position_max_size_pct`` — largest current position size as % of portfolio.
* ``sector_concentration_{sector}`` — per-active-sector concentration.
* ``net_long_pct`` — total long minus total short, signed.
* ``net_short_pct`` — net short magnitude.
* ``gross_exposure_pct`` — sum of absolute positions.

Per-proposal ``contribute`` functions sum across the proposed batch to produce
the rule's total contribution. The projection engine adds the result to
``current`` to derive ``projected_after``. See the story file's per-rule
notes for the contract underlying each implementation.
"""

from __future__ import annotations

from alphamind.risk_guardrails.guardrail_evaluation.rules._helpers import (
    RuleSpec,
    existing_position,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    DeltaAdjustedExposure,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
)

# ---------------------------------------------------------------------------
# position_max_size_pct
# ---------------------------------------------------------------------------


def _position_max_size_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.position_max_size_pct


def _position_max_size_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Contribution = max(0, proposal_size_pct - current_max).

    The rule is *max*, not a sum. The contribution function returns the
    delta from current max to new max if the proposal exceeds the existing
    maximum; 0 otherwise. CLOSE/ADJUST/CANCEL contribute 0 (closes never
    increase max).
    """
    if proposal.action not in (Action.OPEN, Action.ADD):
        return 0.0
    proposal_size_pct = abs(dae.signed_notional_usd) / state.portfolio_value_usd * 100.0
    return max(0.0, proposal_size_pct - state.position_max_size_pct)


# ---------------------------------------------------------------------------
# sector_concentration_{sector}
# ---------------------------------------------------------------------------


def make_sector_concentration_spec(sector: str) -> RuleSpec:
    """Generate a per-sector ``RuleSpec`` capturing the sector key in closures."""

    def read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
        return state.sector_exposure_pct.get(sector, 0.0)

    def contribute(
        proposal: ProposedDelta,
        dae: DeltaAdjustedExposure,
        state: PortfolioStateSnapshot,
        config: LibraryConfig,
    ) -> float:
        if proposal.sector != sector:
            return 0.0
        return dae.signed_notional_usd / state.portfolio_value_usd * 100.0

    return RuleSpec(
        rule_id=f"sector_concentration_{sector}",
        unit="% of portfolio (delta-adjusted)",
        read_current=read_current,
        contribute=contribute,
        effective_limit_key="sector_concentration_pct",
    )


# ---------------------------------------------------------------------------
# net_long_pct
# ---------------------------------------------------------------------------


def _net_long_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.net_long_pct


def _net_long_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Signed contribution: positive ``signed_notional`` increases net long."""
    return dae.signed_notional_usd / state.portfolio_value_usd * 100.0


# ---------------------------------------------------------------------------
# net_short_pct
# ---------------------------------------------------------------------------


def _net_short_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.net_short_pct


def _net_short_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Flip sign: short OPENs (signed_notional<0) contribute positively."""
    return -dae.signed_notional_usd / state.portfolio_value_usd * 100.0


# ---------------------------------------------------------------------------
# gross_exposure_pct
# ---------------------------------------------------------------------------


def _gross_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.gross_pct


def _gross_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Signed change in gross = |after| - |before| over portfolio value.

    OPEN/ADD increase gross by ``|signed_notional|``; CLOSE decreases gross
    by the existing position's notional. ADJUST/CANCEL don't move gross.
    """
    if proposal.action in (Action.OPEN, Action.ADD):
        return abs(dae.signed_notional_usd) / state.portfolio_value_usd * 100.0
    if proposal.action is Action.CLOSE:
        existing = existing_position(proposal, state)
        if existing is None:
            return 0.0
        return -existing.notional_usd / state.portfolio_value_usd * 100.0
    return 0.0


# ---------------------------------------------------------------------------
# Registry entry point
# ---------------------------------------------------------------------------


def exposure_specs() -> tuple[RuleSpec, ...]:
    """Return the static (non-sector) exposure specs.

    Sector-concentration specs are generated per-sector by
    ``make_sector_concentration_spec`` and added by the registry's
    ``build_active_specs``.
    """
    return (
        RuleSpec(
            rule_id="position_max_size_pct",
            unit="% of portfolio",
            read_current=_position_max_size_read_current,
            contribute=_position_max_size_contribute,
            effective_limit_key="position_max_size_pct",
        ),
        RuleSpec(
            rule_id="net_long_pct",
            unit="% of portfolio (delta-adjusted)",
            read_current=_net_long_read_current,
            contribute=_net_long_contribute,
            effective_limit_key="net_long_pct",
        ),
        RuleSpec(
            rule_id="net_short_pct",
            unit="% of portfolio (delta-adjusted)",
            read_current=_net_short_read_current,
            contribute=_net_short_contribute,
            effective_limit_key="net_short_pct",
            requires_shorts=True,
        ),
        RuleSpec(
            rule_id="gross_exposure_pct",
            unit="% of portfolio (delta-adjusted)",
            read_current=_gross_read_current,
            contribute=_gross_contribute,
            effective_limit_key="gross_exposure_pct",
        ),
    )
