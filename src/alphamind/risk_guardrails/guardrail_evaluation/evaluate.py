"""Library entry point — composes the math primitives behind one public function (story 05).

``evaluate_proposals`` accepts a portfolio snapshot, a sequence of proposed
deltas, the carved library config, and market inputs, and returns a
``LibraryOutput`` with per-rule projections, per-proposal delta-adjusted
exposure records, and feature-disabled rejection records. Three callers
compose this output with their own framing (validation tool, proposal
pre-processor, engine T3); the library itself terminates here.

Cross-field invariants on inputs (``option_legs is None ⇔ asset_type ==
EQUITY``, etc.) are checked here — the dataclass in story 01 is constructible
without runtime validation, so the entry point is the single validation seam.
Failures aggregate: every check runs, then one ``LibraryInputError`` is raised
listing every violation.

The function is pure: equal inputs produce equal outputs, no clock reads, no
UUID generation, no logging side effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import MappingProxyType

from alphamind.risk_guardrails.guardrail_evaluation.delta_adjusted import (
    compute_delta_adjusted_exposure,
)
from alphamind.risk_guardrails.guardrail_evaluation.feature_gate import classify_feature_gate
from alphamind.risk_guardrails.guardrail_evaluation.rules import project_all
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    Direction,
    FeatureDisabledRejection,
    LibraryConfig,
    LibraryOutput,
    MarketInputs,
    PortfolioStateSnapshot,
    ProposedDelta,
)


class LibraryInputError(Exception):
    """Raised on cross-field input-validation failures.

    Aggregates every violation into one error so callers fix the full set in
    one pass rather than discovering them one at a time. Mirrors the
    ``CrossReferenceError`` aggregation pattern in
    ``alphamind.config.validation``.
    """


def evaluate_proposals(
    *,
    state: PortfolioStateSnapshot,
    proposals: Sequence[ProposedDelta],
    config: LibraryConfig,
    market: MarketInputs,
) -> LibraryOutput:
    """Project a batch of proposed deltas through the guardrail rules.

    Orchestration:
      1. Validate cross-field invariants on inputs; raise ``LibraryInputError``
         on any failure (aggregated).
      2. For each proposal, classify the feature gate. Feature-disabled
         proposals are added to ``feature_disabled`` and skipped.
      3. For each surviving proposal, compute its delta-adjusted exposure.
      4. Run the rule registry over the surviving proposals.
      5. Assemble ``LibraryOutput``.

    Determinism: equal inputs produce equal outputs (``==`` and ``hash``
    agree). The function is pure — no I/O, no logging, no clock reads.
    """
    _validate_inputs(state=state, proposals=proposals, config=config, market=market)

    rejections: list[FeatureDisabledRejection] = []
    proposals_with_dae: list[tuple[ProposedDelta, DeltaAdjustedExposure]] = []
    for proposal in proposals:
        rejection = classify_feature_gate(proposal, config)
        if rejection is not None:
            rejections.append(rejection)
            continue
        dae = compute_delta_adjusted_exposure(proposal=proposal, market=market, config=config)
        proposals_with_dae.append((proposal, dae))

    projections = project_all(
        proposals_with_dae=proposals_with_dae,
        state=state,
        config=config,
    )

    return LibraryOutput(
        per_rule=projections,
        delta_adjusted=MappingProxyType({p.id: dae for p, dae in proposals_with_dae}),
        feature_disabled=tuple(rejections),
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_inputs(
    *,
    state: PortfolioStateSnapshot,
    proposals: Sequence[ProposedDelta],
    config: LibraryConfig,
    market: MarketInputs,
) -> None:
    """Run every cross-field invariant; raise ``LibraryInputError`` once on any failure."""
    failures: list[str] = []
    if state.portfolio_value_usd <= 0:
        failures.append(f"state.portfolio_value_usd must be > 0; got {state.portfolio_value_usd}")

    seen_ids: set[str] = set()
    for proposal in proposals:
        if proposal.id in seen_ids:
            failures.append(f"duplicate proposal id {proposal.id!r}")
        seen_ids.add(proposal.id)
        failures.extend(_validate_proposal(proposal, state=state, config=config, market=market))

    if failures:
        raise LibraryInputError("\n".join(failures))


def _validate_proposal(
    proposal: ProposedDelta,
    *,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
    market: MarketInputs,
) -> list[str]:
    """Per-proposal cross-field invariants. Returns the list of failure messages."""
    failures: list[str] = []
    failures.extend(_check_asset_type_legs_consistency(proposal))
    failures.extend(_check_action_position_id(proposal, state=state))
    failures.extend(_check_action_sector(proposal, state=state, config=config))
    failures.extend(_check_short_borrow_cost(proposal))
    failures.extend(_check_notional_quantity(proposal))
    failures.extend(_check_underlying_in_market(proposal, market=market))
    return failures


def _check_underlying_in_market(proposal: ProposedDelta, *, market: MarketInputs) -> list[str]:
    """``proposal.underlying`` must be in ``market.underlying_prices``."""
    if proposal.underlying not in market.underlying_prices:
        return [
            f"proposal {proposal.id!r}: underlying {proposal.underlying!r} "
            f"not in market.underlying_prices"
        ]
    return []


def _check_asset_type_legs_consistency(proposal: ProposedDelta) -> list[str]:
    """asset_type vs option_legs: EQUITY ⇔ legs is None; OPTION ⇒ ≥1 leg; STRATEGY ⇒ ≥2 legs."""
    legs = proposal.option_legs
    if proposal.asset_type is AssetType.EQUITY:
        if legs is not None:
            return [f"proposal {proposal.id!r}: EQUITY must not carry option_legs"]
        return []
    if legs is None:
        return [f"proposal {proposal.id!r}: {proposal.asset_type.value} requires option_legs"]
    if proposal.asset_type is AssetType.OPTION and len(legs) < 1:
        return [f"proposal {proposal.id!r}: OPTION requires at least 1 leg"]
    if proposal.asset_type is AssetType.STRATEGY and len(legs) < 2:
        return [f"proposal {proposal.id!r}: STRATEGY requires at least 2 legs"]
    return []


def _check_action_position_id(
    proposal: ProposedDelta, *, state: PortfolioStateSnapshot
) -> list[str]:
    """ADD/CLOSE/ADJUST/CANCEL require a known position id; OPEN must have None.

    CANCEL references the pending-order entry it is releasing — story 04's
    capital rule reads ``existing_position_id`` to subtract the reserved
    capital — so the position id is mandatory there too.
    """
    requires_position = proposal.action in (
        Action.ADD,
        Action.CLOSE,
        Action.ADJUST,
        Action.CANCEL,
    )

    if requires_position:
        if proposal.existing_position_id is None:
            return [
                f"proposal {proposal.id!r}: {proposal.action.value} requires existing_position_id"
            ]
        if proposal.existing_position_id not in state.existing_positions:
            return [
                f"proposal {proposal.id!r}: existing_position_id "
                f"{proposal.existing_position_id!r} not in state.existing_positions"
            ]
    elif proposal.existing_position_id is not None:
        return [
            f"proposal {proposal.id!r}: {proposal.action.value} must not carry existing_position_id"
        ]
    return []


def _check_action_sector(
    proposal: ProposedDelta,
    *,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> list[str]:
    """OPEN must reference an active sector; ADD/CLOSE/ADJUST/CANCEL must match
    the existing position's sector."""
    if proposal.action is Action.OPEN:
        if proposal.sector not in config.active_sectors:
            return [
                f"proposal {proposal.id!r}: sector {proposal.sector!r} "
                f"not in active_sectors {config.active_sectors}"
            ]
        return []
    # Other actions: if the position id resolves, sector must match.
    pos_id = proposal.existing_position_id
    if pos_id is None:
        return []
    existing = state.existing_positions.get(pos_id)
    if existing is None:
        # Already reported by _check_action_position_id; do not duplicate.
        return []
    if proposal.sector != existing.sector:
        return [
            f"proposal {proposal.id!r}: sector {proposal.sector!r} does not match "
            f"existing position {pos_id!r} sector {existing.sector!r}"
        ]
    return []


def _check_short_borrow_cost(proposal: ProposedDelta) -> list[str]:
    """SHORT equity OPEN/ADD requires ``daily_borrow_cost_usd >= 0``."""
    if proposal.direction is not Direction.SHORT:
        return []
    if proposal.asset_type is not AssetType.EQUITY:
        return []
    if proposal.action not in (Action.OPEN, Action.ADD):
        return []
    cost = proposal.daily_borrow_cost_usd
    if cost is None:
        return [
            f"proposal {proposal.id!r}: SHORT EQUITY {proposal.action.value} "
            f"requires daily_borrow_cost_usd"
        ]
    if cost < 0:
        return [f"proposal {proposal.id!r}: daily_borrow_cost_usd must be >= 0; got {cost}"]
    return []


def _check_notional_quantity(proposal: ProposedDelta) -> list[str]:
    """``notional_usd >= 0``; ``quantity > 0``."""
    failures: list[str] = []
    if proposal.notional_usd < 0:
        failures.append(
            f"proposal {proposal.id!r}: notional_usd must be >= 0; got {proposal.notional_usd}"
        )
    if proposal.quantity <= 0:
        failures.append(f"proposal {proposal.id!r}: quantity must be > 0; got {proposal.quantity}")
    return failures
