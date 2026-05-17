"""Shared helpers and the ``RuleSpec`` dataclass for the rule registry (story 04).

Private to the ``rules`` package — these are implementation details, not part
of the library's public API. The registry's per-category modules import
``RuleSpec`` from here at module load time and the public API re-exports it
through ``rules/__init__.py``. Splitting ``RuleSpec`` from the registry's
``__init__`` avoids the circular dependency that would otherwise force per-
category modules into deferred imports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    ExistingPosition,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
)


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """Per-rule data + two function references.

    Fields:

    * ``rule_id`` — string ID matching the key in
      ``LibraryConfig.effective_limits``.
    * ``unit`` — display string for ``RuleProjection.unit``.
    * ``read_current`` — pulls the rule's current value from the snapshot.
    * ``contribute`` — per-proposal contribution (signed, in the rule's units).
    * ``effective_limit_key`` — key into ``LibraryConfig.effective_limits``;
      usually equals ``rule_id``, but the per-sector concentration specs use a
      shared key (``sector_concentration_pct``).
    * ``requires_options`` — rule is only in scope when
      ``feature_flags.options_enabled=True``.
    * ``requires_shorts`` — rule is only in scope when
      ``feature_flags.short_selling_enabled=True``.
    * ``magnitude`` — projection engine classifies on ``|projected_after|``.
    * ``inverse`` — projection engine classifies "below limit = FAIL".
    """

    rule_id: str
    unit: str
    read_current: Callable[[PortfolioStateSnapshot, LibraryConfig], float]
    contribute: Callable[
        [ProposedDelta, DeltaAdjustedExposure, PortfolioStateSnapshot, LibraryConfig],
        float,
    ]
    effective_limit_key: str
    requires_options: bool = False
    requires_shorts: bool = False
    magnitude: bool = False
    inverse: bool = False


def existing_position(
    proposal: ProposedDelta, state: PortfolioStateSnapshot
) -> ExistingPosition | None:
    """Return the ``ExistingPosition`` referenced by ``proposal``, if any.

    ``ADJUST``/``CANCEL`` and ``CLOSE`` proposals carry an
    ``existing_position_id`` referring to a position in ``state``; ``OPEN``
    proposals carry ``None``. Missing IDs return ``None``.
    """
    if proposal.existing_position_id is None:
        return None
    return state.existing_positions.get(proposal.existing_position_id)


def signed_notional_for_contribution(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
) -> float:
    """Signed DAE the DAE-driven rule contributions consume.

    For OPEN/ADD and EQUITY CLOSE, ``dae.signed_notional_usd`` is correct: the
    DAE math signs ``proposal.notional_usd`` for equity closes by the same
    convention as opens, so partial closes scale with the close size.

    For OPTION/STRATEGY CLOSE, the strategist's assessment legitimately omits
    ``option_legs``, so ``compute_delta_adjusted_exposure`` produces a zero
    ``signed_notional_usd``. The rule contribution then reads the existing
    position's stored DAE, negated (the close removes that exposure from the
    book). Returns 0.0 if the position id does not resolve.
    """
    if proposal.action is Action.CLOSE and proposal.asset_type is not AssetType.EQUITY:
        existing = existing_position(proposal, state)
        if existing is None:
            return 0.0
        return -existing.delta_adjusted_exposure_usd
    return dae.signed_notional_usd
