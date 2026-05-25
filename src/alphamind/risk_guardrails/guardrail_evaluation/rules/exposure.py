"""Exposure rule specs (story 04).

Five rules in this category:

* ``position_max_size_pct`` — largest current position size as % of portfolio.
* ``sector_concentration_{sector}`` — per-active-sector concentration.
* ``net_long_pct`` — total long minus total short, signed.
* ``net_short_pct`` — net short magnitude.
* ``gross_exposure_pct`` — sum of absolute positions.

Most exposure rules use the projection engine's default
``contribute``-decomposable path: per-proposal ``contribute`` functions sum
across the proposed batch to produce the rule's total contribution, which the
engine adds to ``current`` to derive ``projected_after``.

``position_max_size_pct`` is the exception. The post-batch maximum position
size is not a sum of per-proposal contributions when multiple positions are
modified in one batch (a CLOSE of the current max doesn't drop the max to
``next-largest`` if a second CLOSE in the same batch also reduced that
position). The rule uses an ``project_after_batch`` holistic projector that
simulates the post-batch position book and computes the new max directly;
its ``contribute`` is a no-op marker that the projection engine never reads.
See ALP-621 for the bug this prevents.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from alphamind.risk_guardrails.guardrail_evaluation.rules._helpers import (
    RuleSpec,
    existing_position,
    signed_notional_for_contribution,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    DeltaAdjustedExposure,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# position_max_size_pct
# ---------------------------------------------------------------------------


def _position_max_size_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.position_max_size_pct


def _position_max_size_no_op_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """No-op ``contribute`` for ``position_max_size_pct``.

    Unreachable in production: the rule's ``RuleSpec`` sets
    ``project_after_batch=_position_max_size_project_after_batch``, and the
    projection engine bypasses ``contribute`` whenever ``project_after_batch``
    is set (see ``project_all`` in ``rules/__init__.py``). The callable
    exists so ``RuleSpec.contribute`` can stay required, keeping the
    registry's per-rule shape uniform.
    """
    return 0.0


def _position_max_size_project_after_batch(
    proposals: Sequence[tuple[ProposedDelta, DeltaAdjustedExposure]],
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """Compute post-batch ``position_max_size_pct`` by simulating the position book.

    The post-batch maximum position size cannot be expressed as a sum of
    per-proposal contributions when multiple positions are modified in one
    batch (ALP-621). This projector mirrors ``library_snapshot``'s actual-max
    derivation so ``projected_after`` matches what ``read_current`` would
    re-compute on the next invocation.

    Simulation rules are encoded in ``_simulate_post_batch_book`` —
    OPEN/ADD/ADJUST/CLOSE/CANCEL semantics mirror the DAE pipeline.

    Guards:

    * ``portfolio_value_usd <= 0.0`` → return ``current`` (snapshot value)
      — defensive; mirrors the snapshot translator and zero-division guards
      throughout the library.
    * Empty post-batch book → return ``0.0``.
    """
    portfolio_value_usd = state.portfolio_value_usd
    if portfolio_value_usd <= 0.0:
        return state.position_max_size_pct

    post_batch_notionals = _simulate_post_batch_book(proposals, state)
    if not post_batch_notionals:
        return 0.0

    max_notional = max(post_batch_notionals)
    return max_notional / portfolio_value_usd * 100.0


def _simulate_post_batch_book(
    proposals: Sequence[tuple[ProposedDelta, DeltaAdjustedExposure]],
    state: PortfolioStateSnapshot,
) -> list[float]:
    """Return the list of post-batch position notionals (absolute USD).

    Simulation rules (mirror the DAE pipeline's action semantics):

    * **CLOSE** on existing position: subtract ``abs(proposal.notional_usd)``
      from the position's notional. If the resulting notional is ``<= 0`` the
      position fully closes and drops out of the book.
    * **ADJUST** on existing position: set the position's notional to
      ``abs(proposal.notional_usd)`` — the proposal carries the new total
      post-ADJUST per the rule's ``contribute`` docstring.
    * **OPEN**: add a synthetic post-batch position with notional
      ``abs(proposal.notional_usd)``.
    * **ADD** on existing position: add ``abs(proposal.notional_usd)`` to the
      existing position's notional.
    * **CANCEL**: no effect on open-position notional.

    Proposals whose ``existing_position_id`` doesn't resolve are skipped
    defensively — validation should have caught them upstream.
    """
    existing_book: dict[str, float] = {
        pid: abs(ep.notional_usd) for pid, ep in state.existing_positions.items()
    }
    opens: list[float] = []
    for proposal, _dae in proposals:
        _apply_proposal_to_book(proposal, existing_book, opens)
    return [*existing_book.values(), *opens]


def _apply_proposal_to_book(
    proposal: ProposedDelta,
    existing_book: dict[str, float],
    opens: list[float],
) -> None:
    """Apply one proposal's effect to the simulated post-batch book.

    Mutates ``existing_book`` and ``opens`` in place. See
    ``_simulate_post_batch_book`` for the per-action semantics.
    """
    action = proposal.action
    if action is Action.OPEN:
        opens.append(abs(float(proposal.notional_usd)))
        return
    if action is Action.CANCEL:
        return
    pos_id = proposal.existing_position_id
    if pos_id is None or pos_id not in existing_book:
        # Defensive — validation should have caught it upstream.
        logger.warning(
            "position_max_size_pct projector skipping proposal id=%r action=%s: "
            "existing_position_id=%r is unresolved in the post-batch book",
            proposal.id,
            proposal.action.name,
            pos_id,
        )
        return
    proposal_notional = abs(float(proposal.notional_usd))
    if action is Action.CLOSE:
        remaining = existing_book[pos_id] - proposal_notional
        if remaining <= 0.0:
            del existing_book[pos_id]
        else:
            existing_book[pos_id] = remaining
    elif action is Action.ADJUST:
        existing_book[pos_id] = proposal_notional
    elif action is Action.ADD:
        existing_book[pos_id] += proposal_notional


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
            contribute=_position_max_size_no_op_contribute,
            project_after_batch=_position_max_size_project_after_batch,
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
