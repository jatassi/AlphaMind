"""Tests for the projection engine (story 04).

The projection engine is a uniform ``project_rule(...)`` that turns
``(rule_id, current, projected_after, effective_limit, zones, unit)`` into a
canonical ``RuleProjection`` with the right ``Status``. The engine has zero
rule-specific knowledge; status classification, headroom math, and unit
propagation are uniform. ``projected_after`` is supplied by the caller —
either as ``current + sum(per_proposal_contributions)`` (the default
contribution-decomposable path the rule registry takes) or as the return
value of a rule's holistic ``project_after_batch`` projector (ALP-621).
"""

from __future__ import annotations

import pytest

from alphamind.risk_guardrails.guardrail_evaluation import (
    EscalationZones,
    ProjectionError,
    RuleProjection,
    Status,
    project_rule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_ZONES = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
_PCT_UNIT = "% of portfolio"


# ---------------------------------------------------------------------------
# Standard classification
# ---------------------------------------------------------------------------


def test_project_rule_pass_below_warning_zone() -> None:
    """consumption_pct < zones.warning → Status.PASS."""
    result = project_rule(
        rule_id="net_long_pct",
        current=5.0,
        projected_after=7.0,
        effective_limit=20.0,
        zones=_DEFAULT_ZONES,
        unit=_PCT_UNIT,
    )
    assert result.status is Status.PASS
    assert result.projected_after == 7.0
    assert result.headroom_remaining == 13.0


def test_project_rule_warning_within_warning_band() -> None:
    """zones.warning <= consumption_pct < zones.hard_block → Status.WARNING."""
    result = project_rule(
        rule_id="net_long_pct",
        current=10.0,
        projected_after=15.0,
        effective_limit=20.0,
        zones=_DEFAULT_ZONES,
        unit=_PCT_UNIT,
    )
    # consumption = 15/20 = 75% → in [70, 95) → WARNING
    assert result.status is Status.WARNING
    assert result.projected_after == 15.0


def test_project_rule_fail_at_or_above_hard_block() -> None:
    """consumption_pct >= zones.hard_block → Status.FAIL."""
    result = project_rule(
        rule_id="net_long_pct",
        current=18.0,
        projected_after=23.0,
        effective_limit=20.0,
        zones=_DEFAULT_ZONES,
        unit=_PCT_UNIT,
    )
    # consumption = 23/20 = 115% → >= 95% → FAIL
    assert result.status is Status.FAIL
    assert result.projected_after == 23.0
    # headroom can be negative on FAIL
    assert result.headroom_remaining == -3.0


# ---------------------------------------------------------------------------
# Defensive guards
# ---------------------------------------------------------------------------


def test_project_rule_zero_limit_raises_projection_error() -> None:
    """``effective_limit == 0`` is a structural error, not a math result."""
    with pytest.raises(ProjectionError):
        project_rule(
            rule_id="net_long_pct",
            current=0.0,
            projected_after=1.0,
            effective_limit=0.0,
            zones=_DEFAULT_ZONES,
            unit=_PCT_UNIT,
        )


def test_project_rule_negative_limit_raises_projection_error() -> None:
    """Negative limits are also structural errors — flips PASS/FAIL classification."""
    with pytest.raises(ProjectionError):
        project_rule(
            rule_id="net_long_pct",
            current=0.0,
            projected_after=1.0,
            effective_limit=-5.0,
            zones=_DEFAULT_ZONES,
            unit=_PCT_UNIT,
        )


# ---------------------------------------------------------------------------
# Magnitude classification (theta, vega)
# ---------------------------------------------------------------------------


def test_project_rule_magnitude_uses_absolute_projected_after() -> None:
    """magnitude=True classifies on |projected_after| / effective_limit."""
    result = project_rule(
        rule_id="portfolio_theta_pct_per_day",
        current=-0.10,
        projected_after=-0.15,
        effective_limit=0.15,
        zones=_DEFAULT_ZONES,
        unit="% of portfolio per day",
        magnitude=True,
    )
    # projected_after = -0.15; |projected_after| = 0.15; consumption = 100% → FAIL
    assert result.status is Status.FAIL
    assert result.projected_after == pytest.approx(-0.15)


def test_project_rule_magnitude_headroom_against_absolute() -> None:
    """For magnitude rules, headroom_remaining = limit - |projected_after|."""
    result = project_rule(
        rule_id="portfolio_theta_pct_per_day",
        current=-0.10,
        projected_after=-0.13,
        effective_limit=0.15,
        zones=_DEFAULT_ZONES,
        unit="% of portfolio per day",
        magnitude=True,
    )
    # |projected_after| = 0.13; headroom = 0.15 - 0.13 = 0.02; consumption ≈ 86.7% → WARNING
    assert result.status is Status.WARNING
    assert result.headroom_remaining == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# Inverse classification (min-cash)
# ---------------------------------------------------------------------------


def test_project_rule_inverse_below_floor_is_fail() -> None:
    """inverse=True: projected_after < effective_limit → FAIL."""
    result = project_rule(
        rule_id="min_cash_reserve_pct",
        current=12.0,
        projected_after=9.0,
        effective_limit=10.0,
        zones=_DEFAULT_ZONES,
        unit="% of portfolio",
        inverse=True,
    )
    # projected_after = 9 < 10 → FAIL
    assert result.status is Status.FAIL
    assert result.projected_after == 9.0


def test_project_rule_inverse_within_warning_band_is_warning() -> None:
    """inverse=True: effective_limit <= projected_after < effective_limit * 1.2 → WARNING."""
    result = project_rule(
        rule_id="min_cash_reserve_pct",
        current=15.0,
        projected_after=10.5,
        effective_limit=10.0,
        zones=_DEFAULT_ZONES,
        unit="% of portfolio",
        inverse=True,
    )
    # projected_after = 10.5; 10 <= 10.5 < 12 → WARNING
    assert result.status is Status.WARNING
    assert result.projected_after == pytest.approx(10.5)


def test_project_rule_inverse_well_above_floor_is_pass() -> None:
    """inverse=True: projected_after >= effective_limit * 1.2 → PASS."""
    result = project_rule(
        rule_id="min_cash_reserve_pct",
        current=20.0,
        projected_after=15.0,
        effective_limit=10.0,
        zones=_DEFAULT_ZONES,
        unit="% of portfolio",
        inverse=True,
    )
    # projected_after = 15 >= 12 → PASS
    assert result.status is Status.PASS
    assert result.projected_after == 15.0


# ---------------------------------------------------------------------------
# Negative-current semantics for non-magnitude rules
# ---------------------------------------------------------------------------


def test_project_rule_negative_consumption_classifies_as_pass() -> None:
    """A net-short book against a max-net-long limit produces negative consumption."""
    result = project_rule(
        rule_id="net_long_pct",
        current=-10.0,
        projected_after=-15.0,
        effective_limit=60.0,
        zones=_DEFAULT_ZONES,
        unit="% of portfolio (delta-adjusted)",
    )
    # projected_after = -15; consumption = -25%; below warning → PASS
    assert result.status is Status.PASS


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


def test_project_rule_returns_canonical_rule_projection_shape() -> None:
    """Return shape mirrors RuleProjection exactly."""
    result = project_rule(
        rule_id="my_rule",
        current=5.0,
        projected_after=10.0,
        effective_limit=20.0,
        zones=_DEFAULT_ZONES,
        unit="USD/day",
    )
    assert isinstance(result, RuleProjection)
    assert result.rule == "my_rule"
    assert result.current == 5.0
    assert result.limit == 20.0
    assert result.projected_after == 10.0
    assert result.headroom_remaining == 10.0
    assert result.unit == "USD/day"


def test_project_rule_default_inverse_is_false() -> None:
    """Standard (non-inverse) rules round-trip ``inverse=False`` through to the
    output projection so callers can distinguish floor vs cap rules without
    maintaining a parallel registry."""
    result = project_rule(
        rule_id="net_long_pct",
        current=5.0,
        projected_after=7.0,
        effective_limit=20.0,
        zones=_DEFAULT_ZONES,
        unit=_PCT_UNIT,
    )
    assert result.inverse is False


def test_project_rule_inverse_true_flag_propagates_to_projection() -> None:
    """Inverse rules round-trip ``inverse=True`` so callers can branch on the
    canonical flag instead of a duplicate registry of rule IDs."""
    result = project_rule(
        rule_id="min_cash_reserve_pct",
        current=15.0,
        projected_after=12.0,
        effective_limit=10.0,
        zones=_DEFAULT_ZONES,
        unit="% of portfolio",
        inverse=True,
    )
    assert result.inverse is True


def test_project_rule_is_pure() -> None:
    """Equal inputs produce equal RuleProjections."""
    a = project_rule(
        rule_id="net_long_pct",
        current=10.0,
        projected_after=15.0,
        effective_limit=20.0,
        zones=_DEFAULT_ZONES,
        unit=_PCT_UNIT,
    )
    b = project_rule(
        rule_id="net_long_pct",
        current=10.0,
        projected_after=15.0,
        effective_limit=20.0,
        zones=_DEFAULT_ZONES,
        unit=_PCT_UNIT,
    )
    assert a == b
