"""Loosening-interpolation primitive (story 04c).

Pure function that, during a ``LOOSENING`` transition, computes the per-rule
interpolated multipliers at any point in the linear interpolation window.
The state machine (story 04b) decides whether the system is loosening and
emits ``transition_invocations_remaining``; this primitive consumes that
count plus the regime multipliers for the origin and destination regimes
and emits the per-rule effective multiplier for the current invocation.

The interpolation is direction-agnostic — it computes the same formula for
loosening (origin > destination) and tightening (origin < destination) — but
the orchestrator only invokes it for ``LOOSENING``. Tightening transitions
apply the destination regime's multipliers immediately.

Reading:
``docs/implementation/06-risk-guardrails/regime-adaptation/04c-loosening-interpolation.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from alphamind._kernel.regime import RegimeTransitionState
from alphamind.risk_guardrails.regime_adaptation.types import LOOSENING_INVOCATIONS


def interpolate_loosening_multipliers(
    *,
    origin_multipliers: Mapping[str, float],
    destination_multipliers: Mapping[str, float],
    transition_invocations_remaining: int,
    loosening_invocations: int = LOOSENING_INVOCATIONS,
) -> Mapping[str, float]:
    """Interpolate per-rule multipliers across the loosening window.

    For each rule present in both maps:

    ``fraction = (loosening_invocations - transition_invocations_remaining + 1)
                 / loosening_invocations``
    ``interpolated = origin + (destination - origin) * fraction``

    Returns a read-only ``MappingProxyType`` view over a fresh dict. The
    inputs are not mutated.
    """
    if loosening_invocations < 1:
        msg = f"loosening_invocations must be >= 1; got {loosening_invocations}"
        raise ValueError(msg)
    if not 1 <= transition_invocations_remaining <= loosening_invocations:
        msg = (
            "transition_invocations_remaining must be in "
            f"[1, loosening_invocations={loosening_invocations}]; got "
            f"transition_invocations_remaining={transition_invocations_remaining}"
        )
        raise ValueError(msg)
    mismatched = set(origin_multipliers).symmetric_difference(destination_multipliers)
    if mismatched:
        msg = (
            "origin_multipliers and destination_multipliers must have identical "
            f"key sets; symmetric difference={sorted(mismatched)!r}"
        )
        raise ValueError(msg)
    for label, multipliers in (
        ("origin_multipliers", origin_multipliers),
        ("destination_multipliers", destination_multipliers),
    ):
        for rule_id, value in multipliers.items():
            if value <= 0:
                msg = (
                    f"{label} multiplier values must be strictly positive; got "
                    f"{label}[{rule_id!r}]={value}"
                )
                raise ValueError(msg)
    fraction = (
        loosening_invocations - transition_invocations_remaining + 1
    ) / loosening_invocations
    interpolated: dict[str, float] = {}
    for rule_id, origin in origin_multipliers.items():
        destination = destination_multipliers[rule_id]
        interpolated[rule_id] = origin + (destination - origin) * fraction
    return MappingProxyType(interpolated)


def resolve_active_multipliers(
    *,
    transition_state: RegimeTransitionState,
    transition_invocations_remaining: int,
    active_regime_multipliers: Mapping[str, float],
    transition_origin_multipliers: Mapping[str, float] | None,
) -> Mapping[str, float]:
    """Dispatch on ``transition_state`` to return either the active or interpolated map.

    - ``STABLE`` and ``TIGHTENING`` return ``active_regime_multipliers``
      verbatim. Tightening applies destination multipliers immediately; stable
      already runs at the destination regime's multipliers.
    - ``LOOSENING`` dispatches to ``interpolate_loosening_multipliers`` with
      origin = ``transition_origin_multipliers``, destination =
      ``active_regime_multipliers``. Raises ``ValueError`` if
      ``transition_origin_multipliers`` is ``None`` (the LOOSENING invariant
      from story 02 requires the origin to be populated).

    Co-located with the interpolation formula so the orchestrator's call site
    stays uniform.
    """
    if transition_state in (RegimeTransitionState.STABLE, RegimeTransitionState.TIGHTENING):
        return active_regime_multipliers
    if transition_origin_multipliers is None:
        msg = (
            "transition_origin_multipliers is required when transition_state "
            "is LOOSENING; got transition_origin_multipliers=None"
        )
        raise ValueError(msg)
    return interpolate_loosening_multipliers(
        origin_multipliers=transition_origin_multipliers,
        destination_multipliers=active_regime_multipliers,
        transition_invocations_remaining=transition_invocations_remaining,
    )
