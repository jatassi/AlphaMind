"""Feature-flag early-exit gate (story 02c).

Per-proposal classifier that returns the canonical ``feature_disabled``
rejection for proposed deltas referencing instrument classes the active
profile disables. The gate is structural: it inspects only ``asset_type`` and
``direction`` against the carved ``FeatureFlagsView`` and returns either a
single-reason rejection or ``None``.

Rationale for the single-reason output: a short option could legitimately be
flagged for both ``options_disabled`` and ``shorts_disabled``, but reporting
both is cosmetic — the proposal is rejected either way. ``options_disabled``
takes precedence (the instrument cannot be opened at all under that flag), so
the feedback loop sees one consistent reason for short-option proposals on
profiles that disable both.

Action-agnosticism is intentional: a ``CLOSE`` of an existing options position
on a profile that has flipped to ``options_enabled=False`` is a
misconfiguration, but the library does not own that policy. Detailed handling
(allow ``CLOSE``-only on disabled features, etc.) lives at the entry point in
story 05.
"""

from __future__ import annotations

from alphamind.risk_guardrails.guardrail_evaluation.types import (
    AssetType,
    Direction,
    FeatureDisabledRejection,
    LibraryConfig,
    ProposedDelta,
)


def classify_feature_gate(
    proposal: ProposedDelta, config: LibraryConfig
) -> FeatureDisabledRejection | None:
    """Return a ``FeatureDisabledRejection`` if ``proposal`` is blocked, else ``None``.

    Total over its inputs; never raises. The check is purely structural — it
    consults ``proposal.asset_type`` and ``proposal.direction`` against
    ``config.feature_flags`` and ignores ``proposal.action``.
    """
    flags = config.feature_flags
    is_options_proposal = proposal.asset_type in (AssetType.OPTION, AssetType.STRATEGY)

    if is_options_proposal and not flags.options_enabled:
        return FeatureDisabledRejection(
            proposal_id=proposal.id,
            reason="options_disabled",
            disabled_feature="options",
        )
    if proposal.direction is Direction.SHORT and not flags.short_selling_enabled:
        return FeatureDisabledRejection(
            proposal_id=proposal.id,
            reason="shorts_disabled",
            disabled_feature="shorts",
        )
    return None
