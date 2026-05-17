"""Shared regime/zone/tier enums consumed across guardrails and distillation.

This module is a leaf within the ``alphamind`` package — it imports nothing
first-party — so it can sit beneath every layer in the import graph. Before
ALP-457 these four ``StrEnum`` definitions were scattered across
``risk_guardrails/regime_adaptation/types.py``,
``risk_guardrails/guardrail_evaluation/types.py``, and
``risk_guardrails/breach_behavior/types.py``; ``portfolio_state``,
``persistence``, and ``distillation`` all consumed them. Because the
risk-guardrails modules in turn imported from ``portfolio_state`` and
``distillation``, the four enums formed the wire for a ten-module import
cycle. Collecting them here breaks that cycle: every consumer imports
downward into ``_kernel`` and the upstream-from-downstream edges disappear.

Each enum's string values are part of the persistence contract — the
``calibration_state``, ``regime_label``, ``regime_transition_state``,
``risk_zone``, and ``drawdown_tier`` CHECK constraints across the SQLite
schema accept exactly these literals.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "DrawdownTier",
    "RegimeLabel",
    "RegimeTransitionState",
    "RiskZone",
    "classify_consumption_zone",
]


class RegimeLabel(StrEnum):
    """Volatility regime classification."""

    LOW_VOL = "LOW_VOL"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    CRISIS = "CRISIS"


class RegimeTransitionState(StrEnum):
    """Whether the system is stable or transitioning between volatility regimes."""

    STABLE = "STABLE"
    TIGHTENING = "TIGHTENING"
    LOOSENING = "LOOSENING"


class RiskZone(StrEnum):
    """Proximity zone for a risk rule limit.

    Produced by the guardrail-evaluation library's per-rule projection and
    consumed by breach-behavior, state-delivery, and capital-state aggregates.
    """

    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    BLOCKED = "BLOCKED"


class DrawdownTier(StrEnum):
    """Cumulative drawdown progressive response tier."""

    CONSTRAINED = "CONSTRAINED"
    HEAVILY_CONSTRAINED = "HEAVILY_CONSTRAINED"
    FULL_HALT = "FULL_HALT"


def classify_consumption_zone(
    *,
    consumption_pct: float,
    warning: float,
    critical: float,
    hard_block: float,
) -> RiskZone:
    """Classify a consumption percentage into a ``RiskZone`` via three thresholds.

    Boundaries are inclusive on the lower bound — ``consumption_pct >= warning``
    classifies as ``WARNING`` (or higher), per the breach-behavior design's
    "70-85% of limit = Warning" wording where each zone owns its lower bound.

    The thresholds must satisfy ``warning < critical < hard_block``; the
    ``EscalationZones`` value objects in both ``breach_behavior.types`` and
    ``guardrail_evaluation.types`` enforce this at construction, and this
    primitive trusts the caller to have passed a validated set.
    """
    if consumption_pct >= hard_block:
        return RiskZone.BLOCKED
    if consumption_pct >= critical:
        return RiskZone.CRITICAL
    if consumption_pct >= warning:
        return RiskZone.WARNING
    return RiskZone.NORMAL
