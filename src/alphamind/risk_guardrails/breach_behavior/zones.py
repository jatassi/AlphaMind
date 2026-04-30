"""Zone classifier (story 04a).

Maps a rule's current value against its limit and per-rule escalation zones to
one of the four ``RiskZone`` enum values. The single deterministic primitive
every zone-tag rendering and zone-aware downstream evaluator composes against.

See ``docs/implementation/06-risk-guardrails/breach-behavior/04a-zone-classifier.md``.
"""

from __future__ import annotations

from alphamind.risk_guardrails.breach_behavior.types import EscalationZones, RiskZone


def classify_zone(
    *,
    current_value: float,
    limit_value: float,
    escalation_zones: EscalationZones,
) -> RiskZone:
    """Classify a rule's current value into one of four risk zones.

    ``consumption_pct = current_value / limit_value * 100``, then:

    * ``consumption_pct < zones.warning``                              -> ``RiskZone.NORMAL``
    * ``zones.warning <= consumption_pct < zones.critical``            -> ``RiskZone.WARNING``
    * ``zones.critical <= consumption_pct < zones.hard_block``         -> ``RiskZone.CRITICAL``
    * ``consumption_pct >= zones.hard_block``                          -> ``RiskZone.BLOCKED``

    Boundaries are inclusive on the lower bound, matching the design doc's
    "70-85% of limit => Warning" wording where each zone owns its lower bound.
    Consumption beyond 100% (current value above the limit) classifies as
    ``BLOCKED`` for any reasonable ``hard_block <= 100``.

    Args:
        current_value: The rule's current value. Must be ``>= 0``.
        limit_value: The rule's effective limit. Must be ``> 0``.
        escalation_zones: The per-rule escalation zone thresholds.

    Returns:
        The ``RiskZone`` the current value falls into.

    Raises:
        ValueError: when ``current_value`` is negative or ``limit_value`` is
            non-positive. The error message names the offending value.
    """
    if current_value < 0:
        msg = f"current_value must be >= 0; got {current_value}"
        raise ValueError(msg)
    if limit_value <= 0:
        msg = f"limit_value must be > 0; got {limit_value}"
        raise ValueError(msg)

    consumption_pct = current_value / limit_value * 100

    if consumption_pct >= escalation_zones.hard_block:
        return RiskZone.BLOCKED
    if consumption_pct >= escalation_zones.critical:
        return RiskZone.CRITICAL
    if consumption_pct >= escalation_zones.warning:
        return RiskZone.WARNING
    return RiskZone.NORMAL
