"""Projection engine (story 04).

The library's deterministic per-rule projection. ``project_rule(...)`` is a
uniform pure function: given a rule's current value, a sequence of signed
contributions, the effective limit, and the rule's escalation zones, it
returns the canonical ``RuleProjection`` with the right ``Status``.

The engine has zero rule-specific knowledge. Status classification, headroom
math, and unit propagation are uniform across all 13 rules covered by the
rule-contribution registry. Rule-specific dispatch happens through two
general-purpose flags:

* ``magnitude=True`` — the rule constrains absolute magnitude (theta, vega).
  Signed contributions sum to a signed ``projected_after``; the engine takes
  ``|projected_after|`` before computing ``consumption_pct``. Headroom is also
  reported against the absolute value.
* ``inverse=True`` — the rule is a *minimum* (e.g., ``min_cash_reserve_pct``).
  Classification flips: below the floor is FAIL; close to the floor is
  WARNING; well above is PASS. The WARNING band width is the
  ``inverse_warning_band_pct`` argument — a percentage of the floor — read
  from ``LibraryConfig.inverse_warning_band_pct`` (ALP-646).

Rules with novel classification needs require either a new flag (rare,
design-doc-driven) or a custom contribution function that pre-shapes the
inputs to one of the existing flag combinations.
"""

from __future__ import annotations

from alphamind.risk_guardrails.guardrail_evaluation.types import (
    EscalationZones,
    RuleProjection,
    Status,
)


class ProjectionError(Exception):
    """Defensive structural-error class.

    Raised on inputs the configuration semantic-self-test should have already
    blocked (e.g., ``effective_limit <= 0``). Math-layer guards against
    structural mistakes that would otherwise produce nonsensical projections.
    """


def project_rule(  # noqa: PLR0913 — math primitive entry point; args mirror the projection inputs
    *,
    rule_id: str,
    current: float,
    projected_after: float,
    effective_limit: float,
    zones: EscalationZones,
    unit: str,
    magnitude: bool = False,
    inverse: bool = False,
    breaching_position_id: str | None = None,
    inverse_warning_band_pct: float = 20.0,
) -> RuleProjection:
    """Pure per-rule projection.

    ``projected_after`` is supplied by the caller — either as
    ``current + sum(per_proposal_contributions)`` (the default path the rule
    registry takes for every contribution-decomposable rule) or as the return
    value of a rule's holistic ``project_after_batch`` projector (used by
    rules whose post-batch value cannot be expressed as a sum of per-proposal
    contributions — e.g., ``position_max_size_pct`` simulates the post-batch
    position book to find the new max; ALP-621).

    Status classification depends on the flags:

    * Standard (``magnitude=False, inverse=False``):
        ``consumption_pct = projected_after / effective_limit * 100``.
        ``< zones.warning`` → PASS;
        ``[zones.warning, zones.hard_block)`` → WARNING;
        ``>= zones.hard_block`` → FAIL. Negative consumption (e.g., net-short
        book vs max-net-long limit) is PASS.
    * Magnitude (``magnitude=True``):
        same thresholds against ``|projected_after| / effective_limit * 100``.
        ``headroom_remaining = effective_limit - |projected_after|``.
    * Inverse (``inverse=True``):
        ``projected_after < effective_limit`` → FAIL;
        ``effective_limit <= projected_after < effective_limit * (1 +
        inverse_warning_band_pct/100)`` → WARNING;
        otherwise PASS. ``headroom_remaining = projected_after -
        effective_limit`` (positive when above the floor).
        ``inverse_warning_band_pct`` is sourced from
        ``LibraryConfig.inverse_warning_band_pct`` (ALP-646); the default
        ``20.0`` matches the historical hardcoded band. The argument is
        ignored on the standard and magnitude paths (``inverse=False``) — it
        is consulted only when an inverse-rule classification is requested.

    Raises ``ProjectionError`` if ``effective_limit <= 0`` — the configuration
    semantic-self-test is supposed to block non-positive limits upstream; the
    math layer's check is a defence-in-depth backstop. Negative limits flip
    the consumption-vs-zone classification, so they're also rejected here.
    """
    if effective_limit <= 0:
        raise ProjectionError(
            f"effective_limit must be > 0 for rule {rule_id!r}; got {effective_limit}"
        )

    status, headroom_remaining = _classify(
        projected_after=projected_after,
        effective_limit=effective_limit,
        zones=zones,
        magnitude=magnitude,
        inverse=inverse,
        inverse_warning_band_pct=inverse_warning_band_pct,
    )

    return RuleProjection(
        rule=rule_id,
        status=status,
        current=current,
        limit=effective_limit,
        projected_after=projected_after,
        headroom_remaining=headroom_remaining,
        unit=unit,
        inverse=inverse,
        breaching_position_id=breaching_position_id,
    )


def _classify(
    *,
    projected_after: float,
    effective_limit: float,
    zones: EscalationZones,
    magnitude: bool,
    inverse: bool,
    inverse_warning_band_pct: float,
) -> tuple[Status, float]:
    """Return (status, headroom_remaining) for the three classification paths."""
    if inverse:
        warning_floor = effective_limit * (1 + inverse_warning_band_pct / 100.0)
        if projected_after < effective_limit:
            status = Status.FAIL
        elif projected_after < warning_floor:
            status = Status.WARNING
        else:
            status = Status.PASS
        headroom_remaining = projected_after - effective_limit
        return status, headroom_remaining

    measured = abs(projected_after) if magnitude else projected_after
    consumption_pct = measured / effective_limit * 100.0

    if consumption_pct < zones.warning:
        status = Status.PASS
    elif consumption_pct < zones.hard_block:
        status = Status.WARNING
    else:
        status = Status.FAIL

    headroom_remaining = effective_limit - measured
    return status, headroom_remaining
