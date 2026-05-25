"""Rule-contribution registry (story 04).

Per-rule ``RuleSpec`` describes how each in-scope rule reads its ``current``
from ``PortfolioStateSnapshot`` and how each ``ProposedDelta`` (with its
precomputed ``DeltaAdjustedExposure``) contributes to the rule.
``build_active_specs(config)`` returns the registry filtered to rules in scope
under the active config; ``project_all(...)`` is the batch projector that
combines the registry with the projection engine.

The registry is a tuple of ``RuleSpec``s, not a class hierarchy: a ``RuleSpec``
is data + two function references. A class-per-rule pattern adds boilerplate
without conveying anything; the closure pattern (per-sector spec generation)
is cleaner with ``RuleSpec`` than with class subclassing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from alphamind.risk_guardrails.guardrail_evaluation.projection import project_rule
from alphamind.risk_guardrails.guardrail_evaluation.rules._helpers import RuleSpec
from alphamind.risk_guardrails.guardrail_evaluation.rules.capital import capital_specs
from alphamind.risk_guardrails.guardrail_evaluation.rules.exposure import (
    exposure_specs,
    make_sector_concentration_spec,
)
from alphamind.risk_guardrails.guardrail_evaluation.rules.options_greeks import (
    options_greeks_specs,
)
from alphamind.risk_guardrails.guardrail_evaluation.rules.shorts import shorts_specs
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    DeltaAdjustedExposure,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleProjection,
)


def build_active_specs(config: LibraryConfig) -> tuple[RuleSpec, ...]:
    """Return the registry filtered to rules in scope under ``config``.

    Filters:

    * Rules absent from ``config.effective_limits`` (under their
      ``effective_limit_key``) are dropped.
    * Rules with ``requires_options=True`` are dropped if
      ``feature_flags.options_enabled=False``.
    * Rules with ``requires_shorts=True`` are dropped if
      ``feature_flags.short_selling_enabled=False``.

    Sector-concentration specs are generated dynamically: one
    ``RuleSpec`` per ``sector ∈ config.active_sectors`` with
    ``rule_id = f"sector_concentration_{sector}"`` (when the
    ``sector_concentration_pct`` limit is present in
    ``effective_limits``).

    Order is stable and lexicographic by ``rule_id`` — entry-point output
    ``per_rule[]`` is ordered by registry order, so equal config inputs
    produce equal output ordering.
    """
    static_specs = (
        *exposure_specs(),
        *options_greeks_specs(),
        *shorts_specs(),
        *capital_specs(),
    )

    sector_specs = _sector_concentration_specs(config)

    candidates = (*static_specs, *sector_specs)
    in_scope = tuple(_filter_in_scope(candidates, config))
    return tuple(sorted(in_scope, key=lambda s: s.rule_id))


def project_all(
    *,
    proposals_with_dae: Iterable[tuple[ProposedDelta, DeltaAdjustedExposure]],
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> tuple[RuleProjection, ...]:
    """Run the registry's projection over every active rule.

    For each spec returned by ``build_active_specs``: read ``current`` from
    the snapshot; compute ``projected_after`` either via the spec's holistic
    ``project_after_batch`` projector (when set — used for rules whose
    post-batch value is not decomposable per-proposal, e.g.,
    ``position_max_size_pct``; ALP-621) or by summing per-proposal
    contributions onto ``current`` (the default contribution-decomposable
    path). Call ``project_rule`` with the resulting ``projected_after``, the
    spec's ``unit``, the effective limit, and the rule's escalation zones.
    Returns the per-rule projections in registry order.
    """
    proposals = tuple(proposals_with_dae)
    specs = build_active_specs(config)
    projections = []
    for spec in specs:
        current = spec.read_current(state, config)
        if spec.project_after_batch is not None:
            projected_after = spec.project_after_batch(proposals, state, config)
        else:
            contributions = [
                spec.contribute(proposal, dae, state, config) for proposal, dae in proposals
            ]
            projected_after = current + math.fsum(contributions)
        breaching_position_id = (
            spec.read_breaching_position_id(state, config)
            if spec.read_breaching_position_id is not None
            else None
        )
        projections.append(
            project_rule(
                rule_id=spec.rule_id,
                current=current,
                projected_after=projected_after,
                effective_limit=config.effective_limits[spec.effective_limit_key],
                zones=config.escalation_zones[spec.effective_limit_key],
                unit=spec.unit,
                magnitude=spec.magnitude,
                inverse=spec.inverse,
                breaching_position_id=breaching_position_id,
                inverse_warning_band_pct=config.inverse_warning_band_pct,
            )
        )
    return tuple(projections)


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------


def _filter_in_scope(specs: Iterable[RuleSpec], config: LibraryConfig) -> Iterable[RuleSpec]:
    flags = config.feature_flags
    for spec in specs:
        if spec.effective_limit_key not in config.effective_limits:
            continue
        if spec.requires_options and not flags.options_enabled:
            continue
        if spec.requires_shorts and not flags.short_selling_enabled:
            continue
        yield spec


def _sector_concentration_specs(config: LibraryConfig) -> tuple[RuleSpec, ...]:
    """Generate one per-sector spec per active sector.

    Each spec captures the sector key in its ``read_current`` and ``contribute``
    closures so the rule reads the right slice of the portfolio snapshot.
    """
    return tuple(make_sector_concentration_spec(sector) for sector in config.active_sectors)


__all__ = [
    "RuleSpec",
    "build_active_specs",
    "project_all",
]
