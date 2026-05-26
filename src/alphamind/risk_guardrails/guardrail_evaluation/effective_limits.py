"""Effective-limit adapter (story 02c).

Bridges the upstream configuration resolver (``compose_config`` in
``alphamind.config.resolver``) to the library's narrower ``LibraryConfig``
shape (defined in story 01). The adapter is a pure read of the already-cascaded
``rule_values`` plus the per-rule ``escalation_zones`` from
``guardrails.yaml`` plus the ``feature_flags`` carved to the library's view.

The adapter does not re-cascade — the regime/overlay/mode multiplication is
``compose_config``'s responsibility. This module trusts the resolver's output
and only validates structural invariants (every rule has an
``escalation_zones`` entry; every active sector exists in ``assets.sectors``;
the conservative buffer is in ``[0, 100]``).
"""

from __future__ import annotations

from alphamind.config.resolver import ResolvedConfig
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    EscalationZones,
    FeatureFlagsView,
    LibraryConfig,
)


class EffectiveLimitAdapterError(Exception):
    """Raised when ``from_resolved_config`` detects a structural inconsistency.

    Each instance carries a single failure message naming the offending field.
    Conditions that raise:

    * A rule in ``rule_values`` is missing from ``guardrails.rules``.
    * A sector in ``active_sectors`` is missing from ``assets.sectors``.
    * ``conservative_delta_buffer_pct`` is outside ``[0, 100]``.

    These are structural errors that should be caught by cross-reference
    validation (story 06a) before reaching the adapter; the runtime check is a
    defence-in-depth backstop.
    """


def from_resolved_config(resolved: ResolvedConfig) -> LibraryConfig:
    """Adapt a ``ResolvedConfig`` to the library's ``LibraryConfig`` view.

    Pure: equal ``ResolvedConfig`` inputs produce equal ``LibraryConfig``
    outputs. Behaviour:

    * ``effective_limits``: the resolver's already-cascaded ``rule_values``
      copied verbatim. The adapter does not re-multiply by regime, overlay,
      or mode — those folds happened upstream in ``compose_config``.
    * ``escalation_zones``: per-rule lookup into ``resolved.guardrails.rules``,
      converted to the library's frozen ``EscalationZones`` dataclass. Rules in
      ``effective_limits`` but missing from ``guardrails.rules`` raise
      ``EffectiveLimitAdapterError``.
    * ``feature_flags``: carved to ``FeatureFlagsView`` (only the two flags the
      library reads — ``fractional_shares_required`` is upstream's concern).
    * ``active_sectors``: ``resolved.profile.active_sectors`` as a tuple,
      order preserved. Sectors absent from ``resolved.assets.sectors`` raise.
    * ``active_regime`` / ``active_profile``: copied from
      ``resolved.regime_label`` / ``resolved.profile_label``.
    * ``conservative_buffer_pct``: copied from
      ``resolved.execution.conservative_delta_buffer_pct``; must lie in
      ``[0, 100]`` or raises.
    * ``position_zones`` / ``inverse_warning_band_pct``: copied from the
      top-level ``guardrails.yaml`` block (ALP-646). The Pydantic schema
      defaults them to ``{70, 85, 95}`` / ``20.0`` when YAML omits them.
    """
    buffer_pct = resolved.execution.conservative_delta_buffer_pct
    if not (0.0 <= buffer_pct <= 100.0):
        raise EffectiveLimitAdapterError(
            f"conservative_delta_buffer_pct must lie in [0, 100], got {buffer_pct}"
        )

    rule_entries = {rule.id: rule for rule in resolved.guardrails.rules}
    escalation_zones: dict[str, EscalationZones] = {}
    for rule_id in resolved.rule_values:
        rule = rule_entries.get(rule_id)
        if rule is None:
            raise EffectiveLimitAdapterError(
                f"Rule {rule_id!r} in rule_values is missing from guardrails.rules"
            )
        zones = rule.escalation_zones
        escalation_zones[rule_id] = EscalationZones(
            warning=float(zones.warning),
            critical=float(zones.critical),
            hard_block=float(zones.hard_block),
        )

    known_sectors = set(resolved.assets.sectors.keys())
    for sector in resolved.profile.active_sectors:
        if sector not in known_sectors:
            raise EffectiveLimitAdapterError(
                f"Active sector {sector!r} is missing from assets.sectors"
            )

    pz = resolved.guardrails.position_zones
    position_zones = EscalationZones(
        warning=float(pz.warning),
        critical=float(pz.critical),
        hard_block=float(pz.hard_block),
    )

    return LibraryConfig(
        effective_limits=dict(resolved.rule_values),
        escalation_zones=escalation_zones,
        feature_flags=FeatureFlagsView(
            options_enabled=resolved.feature_flags.options_enabled,
            short_selling_enabled=resolved.feature_flags.short_selling_enabled,
        ),
        active_sectors=tuple(resolved.profile.active_sectors),
        active_regime=resolved.regime_label,
        active_profile=resolved.profile_label,
        conservative_buffer_pct=float(buffer_pct),
        position_zones=position_zones,
        inverse_warning_band_pct=float(resolved.guardrails.inverse_warning_band_pct),
    )
