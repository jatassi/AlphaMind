"""Cumulative drawdown progressive tier classifier and override applier.

Pure deterministic primitives that translate the per-rule ``progressive_tiers``
configuration into a ``DrawdownTier`` classification and an updated
``ActiveRiskParameterSet`` reflecting the tier's progressive parameter
overrides.

Design reference: ``docs/design/06-risk-guardrails/breach-behavior.md`` §
*Cumulative drawdown response*. The shipped configuration is three tiers —
tier 1 (CONSTRAINED) at 8%, tier 2 (HEAVILY_CONSTRAINED) at 10%, tier 3
(FULL_HALT) at 12%; the classifier is parameterized over the ``progressive_tiers``
sequence so operator tuning does not require code changes.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier

if TYPE_CHECKING:
    # ``ActiveRiskParameter*`` records live in ``portfolio_state.records.capital``,
    # which re-exports ``DrawdownTier`` from this package. Importing them at
    # runtime would cycle through capital → breach_behavior package init → this
    # module. They appear only in annotations, so deferring is sound.
    from alphamind.portfolio_state.aggregates.risk_parameters import (
        ActiveRiskParameterEntry,
        ActiveRiskParameterSet,
    )

_POSITION_MAX_SIZE_RULE_ID = "position_max_size_pct"
_GROSS_EXPOSURE_RULE_ID = "gross_exposure_pct"
_TIER_OVERLAY_PREFIX = "cumulative_drawdown_tier_"

_TIER_TO_NON_HALT_INDEX: dict[DrawdownTier, int] = {
    DrawdownTier.CONSTRAINED: 0,
    DrawdownTier.HEAVILY_CONSTRAINED: 1,
}
"""``DrawdownTier`` to *non-halt* tier-position lookup.

Mirrors the position-based mapping in ``_tier_for_position``. The override
applier resolves the ``ProgressiveTier`` config entry by skipping
``full_halt=True`` entries, then taking the position-th remaining entry."""

_TIER_TO_OVERLAY_NUMBER: dict[DrawdownTier, int] = {
    DrawdownTier.CONSTRAINED: 1,
    DrawdownTier.HEAVILY_CONSTRAINED: 2,
    DrawdownTier.FULL_HALT: 3,
}
"""``DrawdownTier`` to overlay-tag suffix.

The overlay tag format is ``cumulative_drawdown_tier_{N}`` where ``N`` is the
1-indexed canonical tier number."""


def classify_cumulative_drawdown_tier(
    *,
    current_drawdown_pct: float,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> DrawdownTier | None:
    """Classify cumulative drawdown into a progressive response tier.

    Returns ``None`` when ``current_drawdown_pct`` is below every tier's
    ``trigger_pct``. Otherwise returns the ``DrawdownTier`` corresponding to
    the deepest active tier, mapping by *position* in the ``progressive_tiers``
    sequence (decoupled from specific 8/10/12 thresholds — operator tuning
    does not require code changes):

    * The last tier with ``full_halt=True`` maps to ``DrawdownTier.FULL_HALT``.
    * The tier immediately preceding the full-halt tier (if any) maps to
      ``DrawdownTier.HEAVILY_CONSTRAINED``.
    * All earlier tiers map to ``DrawdownTier.CONSTRAINED``.

    The classifier is stateless — recovery has no hysteresis. Per
    ``docs/design/06-risk-guardrails/breach-behavior.md`` § *Cumulative
    drawdown response* — *Recovery*: "Each tier's restrictions exit when
    drawdown depth drops below its threshold. No hysteresis."
    """
    if current_drawdown_pct < 0:
        msg = f"current_drawdown_pct must be >= 0; got {current_drawdown_pct}"
        raise ValueError(msg)
    if not progressive_tiers:
        msg = "progressive_tiers must not be empty"
        raise ValueError(msg)

    # Trigger pcts are monotonically increasing (enforced by GuardrailsConfig);
    # scan from the deepest tier down and return on the first match.
    for index in range(len(progressive_tiers) - 1, -1, -1):
        if current_drawdown_pct >= progressive_tiers[index].trigger_pct:
            return _tier_for_position(index, progressive_tiers)
    return None


def _tier_for_position(
    index: int,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> DrawdownTier:
    """Map a tier *position* in the ordered sequence to a ``DrawdownTier``.

    The mapping uses the position rather than the ``trigger_pct`` value so the
    classifier and override applier remain decoupled from the specific 8/10/12
    thresholds.

    The tier-position to enum map counts non-halt tiers from the bottom: the
    first non-halt tier is ``CONSTRAINED``, the second non-halt tier is
    ``HEAVILY_CONSTRAINED``, and any ``full_halt=True`` tier is ``FULL_HALT``.
    Configurations omitting a middle tier (e.g., 8/12-only) skip
    ``HEAVILY_CONSTRAINED`` entirely.
    """
    if progressive_tiers[index].full_halt:
        return DrawdownTier.FULL_HALT
    non_halt_position = sum(1 for tier_cfg in progressive_tiers[:index] if not tier_cfg.full_halt)
    if non_halt_position == 0:
        return DrawdownTier.CONSTRAINED
    return DrawdownTier.HEAVILY_CONSTRAINED


def apply_progressive_tier_overrides(
    *,
    active_risk_parameters: ActiveRiskParameterSet,
    tier: DrawdownTier | None,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> ActiveRiskParameterSet:
    """Return an ``ActiveRiskParameterSet`` with per-tier overrides applied.

    Tier 1 (``CONSTRAINED``) and tier 2 (``HEAVILY_CONSTRAINED``) replace the
    ``position_max_size_pct`` and ``gross_exposure_pct`` rule values with the
    tier's ``max_position_size_pct`` and ``max_gross_pct`` configuration. The
    override never loosens — when the input rule value is already tighter than
    the tier override (e.g., crisis regime), ``min(regime_value, tier_value)``
    semantics apply.

    Tier 3 (``FULL_HALT``) does not override individual rule values; the
    full-halt response blocks new OPEN/ADD entirely (enforced by the engine
    halt-mode action vocabulary). Per-rule values are preserved and only the
    ``cumulative_drawdown_tier_3`` overlay tag is appended.

    All non-overridden entries flow through unchanged. The returned
    parameter set preserves ``regime_label``, ``transition_state``,
    ``transition_invocations_remaining``, and ``parameter_change_flag``. The
    ``active_overlays`` tuple is extended with ``cumulative_drawdown_tier_{N}``
    and re-sorted alphabetically (idempotent — applying the same tier twice
    does not duplicate the tag).
    """
    if tier is None:
        return active_risk_parameters

    overlays = _extend_overlays(active_risk_parameters.active_overlays, tier)

    if tier is DrawdownTier.FULL_HALT:
        return dataclasses.replace(active_risk_parameters, active_overlays=overlays)

    tier_cfg = _resolve_non_halt_tier(tier, progressive_tiers)
    new_entries = _override_rule_values(active_risk_parameters.entries, tier_cfg)
    return dataclasses.replace(
        active_risk_parameters, entries=new_entries, active_overlays=overlays
    )


def _extend_overlays(existing: tuple[str, ...], tier: DrawdownTier) -> tuple[str, ...]:
    """Append the tier overlay tag and return the alphabetically-sorted tuple."""
    tag = f"{_TIER_OVERLAY_PREFIX}{_TIER_TO_OVERLAY_NUMBER[tier]}"
    if tag in existing:
        return existing
    return tuple(sorted((*existing, tag)))


def _resolve_non_halt_tier(
    tier: DrawdownTier,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> ProgressiveTier:
    """Return the ``ProgressiveTier`` configuration entry corresponding to
    a non-halt ``DrawdownTier``.

    Skips ``full_halt=True`` entries to mirror the classifier's position-based
    mapping. Raises ``ValueError`` if the configuration does not carry the
    requested tier (e.g., ``HEAVILY_CONSTRAINED`` requested but only one
    non-halt tier defined).
    """
    target_index = _TIER_TO_NON_HALT_INDEX[tier]
    non_halt = [t for t in progressive_tiers if not t.full_halt]
    if target_index >= len(non_halt):
        msg = (
            f"progressive_tiers does not carry a tier matching {tier.value!r}; "
            f"need at least {target_index + 1} non-halt tier(s), got {len(non_halt)}"
        )
        raise ValueError(msg)
    return non_halt[target_index]


def _override_rule_values(
    entries: tuple[ActiveRiskParameterEntry, ...],
    tier_cfg: ProgressiveTier,
) -> tuple[ActiveRiskParameterEntry, ...]:
    """Apply tier-1/tier-2 overrides to the position-size and gross-exposure
    entries.

    The override never loosens: when the input value is already tighter than
    the tier limit, the input value is preserved (``min(regime, tier)``).
    Raises ``KeyError`` when either rule entry is missing.
    """
    overrides: dict[str, float] = {}
    if tier_cfg.max_position_size_pct is not None:
        overrides[_POSITION_MAX_SIZE_RULE_ID] = tier_cfg.max_position_size_pct
    if tier_cfg.max_gross_pct is not None:
        overrides[_GROSS_EXPOSURE_RULE_ID] = tier_cfg.max_gross_pct

    rule_ids_present = {entry.rule_id for entry in entries}
    for required_id in (_POSITION_MAX_SIZE_RULE_ID, _GROSS_EXPOSURE_RULE_ID):
        if required_id not in rule_ids_present:
            msg = f"active_risk_parameters missing required entry {required_id!r}"
            raise KeyError(msg)

    return tuple(
        dataclasses.replace(
            entry,
            value=min(entry.value, overrides[entry.rule_id]),
            regime_multiplier_applied=1.0,
        )
        if entry.rule_id in overrides
        else entry
        for entry in entries
    )
