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
