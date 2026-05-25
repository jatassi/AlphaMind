"""Shared helpers and the ``RuleSpec`` dataclass for the rule registry (story 04).

Private to the ``rules`` package — these are implementation details, not part
of the library's public API. The registry's per-category modules import
``RuleSpec`` from here at module load time and the public API re-exports it
through ``rules/__init__.py``. Splitting ``RuleSpec`` from the registry's
``__init__`` avoids the circular dependency that would otherwise force per-
category modules into deferred imports.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    ExistingPosition,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposalContribution,
    ProposedDelta,
)


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """Per-rule data + function references for the projection engine.

    Fields:

    * ``rule_id`` — string ID matching the key in
      ``LibraryConfig.effective_limits``.
    * ``unit`` — display string for ``RuleProjection.unit``.
    * ``read_current`` — pulls the rule's current value from the snapshot.
    * ``contribute`` — per-proposal contribution (signed, in the rule's units).
      Required for every rule. When ``project_after_batch`` is also set, the
      projection engine bypasses ``contribute`` and uses the batch projector
      directly; the rule still provides a ``contribute`` callable so the type
      stays uniform across the registry.
    * ``project_after_batch`` — optional holistic batch projector. When set,
      the projection engine calls it with the full proposals tuple and uses
      its return value as ``projected_after`` directly, bypassing the
      ``current + sum(contribute(...))`` model. Use for rules whose
      post-batch value is not a simple sum of per-proposal contributions
      (e.g., ``position_max_size_pct`` must simulate the post-batch position
      book to find the new max, which is not decomposable per-proposal when
      multiple positions are closed in one batch — ALP-621).
    * ``contributors_from_batch`` — optional holistic contributor projector.
      Returns the per-proposal attribution that the proposal pre-processor's
      breach-entry construction surfaces. When set, the pre-processor uses
      it instead of walking ``spec.contribute(...)`` per proposal. Required
      whenever ``project_after_batch`` is set, since the corresponding
      ``contribute`` is a no-op marker (see ALP-636).
    * ``effective_limit_key`` — key into ``LibraryConfig.effective_limits``;
      usually equals ``rule_id``, but the per-sector concentration specs use a
      shared key (``sector_concentration_pct``).
    * ``requires_options`` — rule is only in scope when
      ``feature_flags.options_enabled=True``.
    * ``requires_shorts`` — rule is only in scope when
      ``feature_flags.short_selling_enabled=True``.
    * ``magnitude`` — projection engine classifies on ``|projected_after|``.
    * ``inverse`` — projection engine classifies "below limit = FAIL".
    * ``read_breaching_position_id`` — optional projector for the
      ``position_id`` of the position whose state triggered the rule. The
      rule's ``read_breaching_position_id`` reads a pre-computed id off the
      snapshot (``PortfolioStateSnapshot`` carries one per per-position
      rule); the continuous-monitor cascade dispatcher routes the breach
      close to that id without re-scanning. Only ``single_short_max_pct``
      wires it today; portfolio-scope rules (drawdown, gross, net) leave
      it ``None``.
    """

    rule_id: str
    unit: str
    read_current: Callable[[PortfolioStateSnapshot, LibraryConfig], float]
    contribute: Callable[
        [ProposedDelta, DeltaAdjustedExposure, PortfolioStateSnapshot, LibraryConfig],
        float,
    ]
    effective_limit_key: str
    project_after_batch: (
        Callable[
            [
                Sequence[tuple[ProposedDelta, DeltaAdjustedExposure]],
                PortfolioStateSnapshot,
                LibraryConfig,
            ],
            float,
        ]
        | None
    ) = None
    contributors_from_batch: (
        Callable[
            [
                Sequence[tuple[ProposedDelta, DeltaAdjustedExposure]],
                PortfolioStateSnapshot,
                LibraryConfig,
            ],
            tuple[ProposalContribution, ...],
        ]
        | None
    ) = None
    requires_options: bool = False
    requires_shorts: bool = False
    magnitude: bool = False
    inverse: bool = False
    read_breaching_position_id: (
        Callable[[PortfolioStateSnapshot, LibraryConfig], str | None] | None
    ) = None

    def __post_init__(self) -> None:
        # Holistic-rule pairing: a rule that bypasses ``contribute`` via
        # ``project_after_batch`` must also provide ``contributors_from_batch``
        # for the proposal pre-processor — otherwise ``_build_breach_entry``
        # falls back to walking the no-op ``contribute`` and silently emits
        # empty contributors (ALP-636).
        if self.project_after_batch is not None and self.contributors_from_batch is None:
            raise ValueError(
                f"RuleSpec(rule_id={self.rule_id!r}): project_after_batch is set "
                "but contributors_from_batch is missing; holistic rules must "
                "provide both (see ALP-636)"
            )


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
