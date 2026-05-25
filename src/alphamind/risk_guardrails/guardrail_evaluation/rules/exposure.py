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
    signed_notional_for_contribution,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    DeltaAdjustedExposure,
    ExistingPosition,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
)

# ---------------------------------------------------------------------------
# position_max_size_pct
# ---------------------------------------------------------------------------


def _position_max_size_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.position_max_size_pct


def _next_largest_position_size_pct(
    state: PortfolioStateSnapshot, excluded_position_id: str
) -> float:
    """Return the largest ``|notional_usd| / portfolio_value * 100`` across
    ``state.existing_positions`` excluding ``excluded_position_id``.

    Returns ``0.0`` when no other positions exist (the close empties the book
    from the rule's perspective) or when ``portfolio_value_usd`` is zero
    (mirrors the ``library_snapshot`` and ``_breach_magnitude`` guards).

    The formula matches ``to_library_snapshot``'s actual-max derivation so the
    rule's projected-after value stays consistent with how ``read_current``
    would re-compute the state on the next invocation.
    """
    if state.portfolio_value_usd <= 0.0:
        return 0.0
    others = [
        ep
        for pid, ep in state.existing_positions.items()
        if pid != excluded_position_id
    ]
    if not others:
        return 0.0
    return max(abs(ep.notional_usd) for ep in others) / state.portfolio_value_usd * 100.0


def _position_max_size_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Signed contribution to ``position_max_size_pct``.

    The rule tracks the largest position's size; the projection engine sums
    contributions onto ``state.position_max_size_pct`` to derive
    ``projected_after``.

    Action-by-action semantics (ALP-624):

    * **OPEN / ADD** — ``max(0, proposal_size_pct - current_max)``. When the
      new (or grown) position would exceed the current max, the delta lifts the
      rule's max to the new size; otherwise 0.
    * **CLOSE** — if the proposal targets the current-max position, the
      contribution is ``next_largest_pct - current_max_pct`` (a non-positive
      number that drops ``projected_after`` to the next-largest position's
      size, or to 0 when the closed position was the only one). If the
      proposal targets a non-max position, contribution is 0. Unresolved
      ``existing_position_id`` is treated as a non-max close (contribution 0)
      so the function stays total.
    * **ADJUST** — depends on whether the adjustment shrinks or grows the
      current-max position. If the proposal targets the current-max:
      ``proposal.notional_usd < existing.notional_usd`` (shrink) behaves like
      CLOSE-down-to-new-size capped by ``next_largest`` (the rule's max becomes
      ``max(new_size_pct, next_largest_pct)``); ``proposal.notional_usd >
      existing.notional_usd`` (grow) behaves like ADD on top of the current
      max (``new_size_pct - current_max`` when positive). An ADJUST on a
      non-max position only lifts the max when its post-adjust size exceeds
      the current max.
    * **CANCEL** — 0 (cancel releases reserved capital but doesn't change
      open-position notional).
    """
    if proposal.action in (Action.OPEN, Action.ADD):
        proposal_size_pct = abs(dae.signed_notional_usd) / state.portfolio_value_usd * 100.0
        return max(0.0, proposal_size_pct - state.position_max_size_pct)

    if proposal.action is Action.CANCEL:
        return 0.0

    existing = existing_position(proposal, state)
    if existing is None:
        return 0.0

    current_max_pct = state.position_max_size_pct
    existing_size_pct = abs(existing.notional_usd) / state.portfolio_value_usd * 100.0
    is_current_max = _is_current_max_position(existing_size_pct, current_max_pct)

    if proposal.action is Action.CLOSE:
        if not is_current_max:
            return 0.0
        next_largest_pct = _next_largest_position_size_pct(state, existing.position_id)
        return next_largest_pct - current_max_pct

    if proposal.action is Action.ADJUST:
        return _adjust_contribution(
            proposal=proposal,
            existing=existing,
            state=state,
            existing_size_pct=existing_size_pct,
            current_max_pct=current_max_pct,
            is_current_max=is_current_max,
        )

    return 0.0


def _is_current_max_position(existing_size_pct: float, current_max_pct: float) -> bool:
    """Return whether the existing position matches the current-max bucket.

    Float-tolerant comparison — the snapshot's ``position_max_size_pct`` was
    derived from the same ``|notional| / portfolio_value * 100`` formula, but
    intermediate float arithmetic can drift by ~1e-12; an exact ``==`` would
    misclassify the current-max position as non-max on rare inputs.
    """
    return abs(existing_size_pct - current_max_pct) <= 1e-9


def _adjust_contribution(
    *,
    proposal: ProposedDelta,
    existing: ExistingPosition,
    state: PortfolioStateSnapshot,
    existing_size_pct: float,
    current_max_pct: float,
    is_current_max: bool,
) -> float:
    """ADJUST contribution to ``position_max_size_pct``.

    The DAE pipeline returns ``signed_notional_usd=0`` for ADJUST (it is
    exposure-neutral by definition in the DAE math), so this helper derives
    the post-adjust size directly from ``proposal.notional_usd`` against
    ``existing.notional_usd``.
    """
    new_size_pct = abs(float(proposal.notional_usd)) / state.portfolio_value_usd * 100.0
    if is_current_max:
        # Shrink or grow: the rule's max becomes either the new size or the
        # next-largest position, whichever is larger.
        next_largest_pct = _next_largest_position_size_pct(state, existing.position_id)
        new_max_pct = max(new_size_pct, next_largest_pct)
        return new_max_pct - current_max_pct
    # Adjust on a non-max position only lifts the max when its post-adjust
    # size exceeds the current max.
    return max(0.0, new_size_pct - current_max_pct)


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
        signed = signed_notional_for_contribution(proposal, dae, state)
        return signed / state.portfolio_value_usd * 100.0

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
    signed = signed_notional_for_contribution(proposal, dae, state)
    return signed / state.portfolio_value_usd * 100.0


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
    signed = signed_notional_for_contribution(proposal, dae, state)
    return -signed / state.portfolio_value_usd * 100.0


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
