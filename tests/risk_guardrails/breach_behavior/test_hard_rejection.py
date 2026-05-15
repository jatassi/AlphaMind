"""Tests for ``compose_hard_rejection_payload`` (story 04c).

Stub objects deliberately satisfy the documented Protocols structurally — the
production guardrail-evaluation ``RuleProjection`` and ``LibraryOutput`` shapes
are compatible. Tests do not import the production types so this story stays
decoupled from the upstream layer.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from alphamind.risk_guardrails.breach_behavior import (
    RejectionRuleEntry,
    compose_hard_rejection_payload,
)


@dataclass(frozen=True)
class _StubRuleProjection:
    """Structural stand-in for ``RuleProjectionProtocol``."""

    rule: str
    status: str
    current: float
    limit: float
    projected_after: float
    headroom_remaining: float
    unit: str
    inverse: bool = False


@dataclass(frozen=True)
class _StubLibraryOutput:
    """Structural stand-in for ``LibraryOutputProtocol``."""

    per_rule: tuple[_StubRuleProjection, ...]


# ---------------------------------------------------------------------------
# Tracer bullet: single-rule sector-concentration rejection
# ---------------------------------------------------------------------------


def test_single_rule_rejection_carries_breaching_rule() -> None:
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="net_long_pct",
                status="PASS",
                current=42.0,
                limit=60.0,
                projected_after=44.5,
                headroom_remaining=15.5,
                unit="% of portfolio (delta-adjusted)",
            ),
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="FAIL",
                current=23.7,
                limit=25.0,
                projected_after=28.2,
                headroom_remaining=-3.2,
                unit="% of portfolio (delta-adjusted)",
            ),
        ),
    )
    after_compliance = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="net_long_pct",
                status="PASS",
                current=42.0,
                limit=60.0,
                projected_after=43.4,
                headroom_remaining=16.6,
                unit="% of portfolio (delta-adjusted)",
            ),
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="WARNING",
                current=23.7,
                limit=25.0,
                projected_after=24.5,
                headroom_remaining=0.5,
                unit="% of portfolio (delta-adjusted)",
            ),
        ),
    )

    payload = compose_hard_rejection_payload(
        rejected_command_id="PM.20260428T100000Z.1.1",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="reduce size by 42%",
    )

    assert payload.rejected_command_id == "PM.20260428T100000Z.1.1"
    assert payload.suggested_modification == "reduce size by 42%"
    assert len(payload.breaching_rules) == 1
    assert payload.breaching_rules[0].rule_id == "sector_concentration_tech"
    assert payload.breaching_rules[0].current_value == 23.7
    assert payload.breaching_rules[0].limit_value == 25.0
    assert payload.breaching_rules[0].projected_after == 28.2
    assert payload.breaching_rules[0].overage == pytest.approx(3.2)
    assert payload.breaching_rules[0].headroom_remaining == pytest.approx(-3.2)
    assert payload.breaching_rules[0].unit == "% of portfolio (delta-adjusted)"


# ---------------------------------------------------------------------------
# Multi-rule rejection — iteration order preserved
# ---------------------------------------------------------------------------


def test_multi_rule_rejection_preserves_iteration_order() -> None:
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="options_delta_pct",
                status="FAIL",
                current=18.0,
                limit=20.0,
                projected_after=22.5,
                headroom_remaining=-2.5,
                unit="% of portfolio",
            ),
            _StubRuleProjection(
                rule="portfolio_theta_pct_per_day",
                status="FAIL",
                current=0.4,
                limit=0.5,
                projected_after=0.55,
                headroom_remaining=-0.05,
                unit="% of portfolio per day",
            ),
            _StubRuleProjection(
                rule="portfolio_vega_pct_per_iv_point",
                status="PASS",
                current=1.0,
                limit=2.0,
                projected_after=1.2,
                headroom_remaining=0.8,
                unit="% of portfolio per 1-pt IV move",
            ),
        ),
    )
    after_compliance = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="options_delta_pct",
                status="PASS",
                current=18.0,
                limit=20.0,
                projected_after=19.0,
                headroom_remaining=1.0,
                unit="% of portfolio",
            ),
            _StubRuleProjection(
                rule="portfolio_theta_pct_per_day",
                status="PASS",
                current=0.4,
                limit=0.5,
                projected_after=0.45,
                headroom_remaining=0.05,
                unit="% of portfolio per day",
            ),
            _StubRuleProjection(
                rule="portfolio_vega_pct_per_iv_point",
                status="PASS",
                current=1.0,
                limit=2.0,
                projected_after=1.05,
                headroom_remaining=0.95,
                unit="% of portfolio per 1-pt IV move",
            ),
        ),
    )

    payload = compose_hard_rejection_payload(
        rejected_command_id="PM.20260428T100000Z.1.2",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="switch to a lower-delta strike",
    )

    assert tuple(entry.rule_id for entry in payload.breaching_rules) == (
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
    )
    # Post-compliance set carries every rule, breaching or not.
    assert tuple(entry.rule_id for entry in payload.headroom_after_hypothetical_compliance) == (
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
    )


def test_iteration_order_preserved_with_interleaved_pass_and_fail() -> None:
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="rule_a",
                status="PASS",
                current=0.0,
                limit=10.0,
                projected_after=1.0,
                headroom_remaining=9.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_b",
                status="FAIL",
                current=8.0,
                limit=10.0,
                projected_after=11.0,
                headroom_remaining=-1.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_c",
                status="PASS",
                current=2.0,
                limit=10.0,
                projected_after=3.0,
                headroom_remaining=7.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_d",
                status="FAIL",
                current=6.0,
                limit=10.0,
                projected_after=12.0,
                headroom_remaining=-2.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_e",
                status="WARNING",
                current=7.0,
                limit=10.0,
                projected_after=9.0,
                headroom_remaining=1.0,
                unit="x",
            ),
        ),
    )
    after_compliance = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="rule_a",
                status="PASS",
                current=0.0,
                limit=10.0,
                projected_after=0.5,
                headroom_remaining=9.5,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_b",
                status="PASS",
                current=8.0,
                limit=10.0,
                projected_after=9.0,
                headroom_remaining=1.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_c",
                status="PASS",
                current=2.0,
                limit=10.0,
                projected_after=2.5,
                headroom_remaining=7.5,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_d",
                status="PASS",
                current=6.0,
                limit=10.0,
                projected_after=8.0,
                headroom_remaining=2.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_e",
                status="PASS",
                current=7.0,
                limit=10.0,
                projected_after=7.5,
                headroom_remaining=2.5,
                unit="x",
            ),
        ),
    )

    payload = compose_hard_rejection_payload(
        rejected_command_id="PM.20260428T100000Z.1.3",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="reduce notional",
    )

    # FAILs at indices 1, 3 — output order matches input order, skipping
    # PASS/WARNING entries.
    assert tuple(entry.rule_id for entry in payload.breaching_rules) == ("rule_b", "rule_d")


# ---------------------------------------------------------------------------
# Post-compliance overage semantics
# ---------------------------------------------------------------------------


def test_cured_rule_post_compliance_overage_is_zero() -> None:
    """Post-compliance ``projected_after < limit`` ⇒ overage clamped to 0."""
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="FAIL",
                current=23.7,
                limit=25.0,
                projected_after=28.2,
                headroom_remaining=-3.2,
                unit="% of portfolio",
            ),
        ),
    )
    after_compliance = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="PASS",
                current=23.7,
                limit=25.0,
                projected_after=24.0,
                headroom_remaining=1.0,
                unit="% of portfolio",
            ),
        ),
    )

    payload = compose_hard_rejection_payload(
        rejected_command_id="PM.20260428T100000Z.1.4",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="reduce size",
    )

    sector_post = payload.headroom_after_hypothetical_compliance[0]
    assert sector_post.overage == 0.0
    assert sector_post.headroom_remaining == pytest.approx(1.0)


def test_partial_cure_surfaces_remaining_overage() -> None:
    """Post-compliance ``projected_after > limit`` ⇒ overage > 0 (PM sees partial cure)."""
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="FAIL",
                current=23.7,
                limit=25.0,
                projected_after=30.0,
                headroom_remaining=-5.0,
                unit="% of portfolio",
            ),
            _StubRuleProjection(
                rule="portfolio_theta_pct_per_day",
                status="FAIL",
                current=0.4,
                limit=0.5,
                projected_after=0.7,
                headroom_remaining=-0.2,
                unit="% of portfolio per day",
            ),
        ),
    )
    after_compliance = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="PASS",
                current=23.7,
                limit=25.0,
                projected_after=24.0,
                headroom_remaining=1.0,
                unit="% of portfolio",
            ),
            _StubRuleProjection(
                rule="portfolio_theta_pct_per_day",
                status="FAIL",
                current=0.4,
                limit=0.5,
                projected_after=0.6,
                headroom_remaining=-0.1,
                unit="% of portfolio per day",
            ),
        ),
    )

    payload = compose_hard_rejection_payload(
        rejected_command_id="PM.20260428T100000Z.1.5",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="reduce size",
    )

    sector_post = payload.headroom_after_hypothetical_compliance[0]
    theta_post = payload.headroom_after_hypothetical_compliance[1]
    assert sector_post.overage == 0.0
    assert theta_post.overage == pytest.approx(0.1)
    assert theta_post.headroom_remaining == pytest.approx(-0.1)


def test_post_compliance_exactly_at_limit_zero_overage_zero_headroom() -> None:
    """Boundary: ``projected_after == limit`` ⇒ overage=0 and headroom=0."""
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="rule_x",
                status="FAIL",
                current=8.0,
                limit=10.0,
                projected_after=11.0,
                headroom_remaining=-1.0,
                unit="x",
            ),
        ),
    )
    after_compliance = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="rule_x",
                status="WARNING",
                current=8.0,
                limit=10.0,
                projected_after=10.0,
                headroom_remaining=0.0,
                unit="x",
            ),
        ),
    )

    payload = compose_hard_rejection_payload(
        rejected_command_id="PM.20260428T100000Z.1.6",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="reduce size to exact limit",
    )

    rule_x_post = payload.headroom_after_hypothetical_compliance[0]
    assert rule_x_post.overage == 0.0
    assert rule_x_post.headroom_remaining == 0.0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_VALID_AFTER_COMPLIANCE = _StubLibraryOutput(
    per_rule=(
        _StubRuleProjection(
            rule="rule_x",
            status="PASS",
            current=1.0,
            limit=10.0,
            projected_after=2.0,
            headroom_remaining=8.0,
            unit="x",
        ),
    ),
)


def test_no_failed_rules_raises_value_error_naming_absence() -> None:
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="rule_a",
                status="PASS",
                current=1.0,
                limit=10.0,
                projected_after=2.0,
                headroom_remaining=8.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_b",
                status="PASS",
                current=3.0,
                limit=10.0,
                projected_after=4.0,
                headroom_remaining=6.0,
                unit="x",
            ),
            _StubRuleProjection(
                rule="rule_c",
                status="WARNING",
                current=8.0,
                limit=10.0,
                projected_after=9.0,
                headroom_remaining=1.0,
                unit="x",
            ),
        ),
    )
    with pytest.raises(ValueError, match="no rules failed"):
        compose_hard_rejection_payload(
            rejected_command_id="PM.X.1.1",
            library_output=library_output,
            library_output_after_hypothetical_compliance=_VALID_AFTER_COMPLIANCE,
            suggested_modification="trim",
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n  "])
def test_blank_suggested_modification_raises(blank: str) -> None:
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="rule_x",
                status="FAIL",
                current=8.0,
                limit=10.0,
                projected_after=11.0,
                headroom_remaining=-1.0,
                unit="x",
            ),
        ),
    )
    with pytest.raises(ValueError, match="suggested_modification"):
        compose_hard_rejection_payload(
            rejected_command_id="PM.X.1.1",
            library_output=library_output,
            library_output_after_hypothetical_compliance=_VALID_AFTER_COMPLIANCE,
            suggested_modification=blank,
        )


def test_empty_library_output_per_rule_raises() -> None:
    with pytest.raises(ValueError, match=r"library_output\.per_rule is empty"):
        compose_hard_rejection_payload(
            rejected_command_id="PM.X.1.1",
            library_output=_StubLibraryOutput(per_rule=()),
            library_output_after_hypothetical_compliance=_VALID_AFTER_COMPLIANCE,
            suggested_modification="trim",
        )


def test_empty_library_output_after_hypothetical_compliance_per_rule_raises() -> None:
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="rule_x",
                status="FAIL",
                current=8.0,
                limit=10.0,
                projected_after=11.0,
                headroom_remaining=-1.0,
                unit="x",
            ),
        ),
    )
    with pytest.raises(ValueError, match="post-compliance picture required"):
        compose_hard_rejection_payload(
            rejected_command_id="PM.X.1.1",
            library_output=library_output,
            library_output_after_hypothetical_compliance=_StubLibraryOutput(per_rule=()),
            suggested_modification="trim",
        )


# ---------------------------------------------------------------------------
# Determinism, purity, frozen output
# ---------------------------------------------------------------------------


def _build_canonical_inputs() -> tuple[_StubLibraryOutput, _StubLibraryOutput]:
    library_output = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="FAIL",
                current=23.7,
                limit=25.0,
                projected_after=28.2,
                headroom_remaining=-3.2,
                unit="% of portfolio (delta-adjusted)",
            ),
        ),
    )
    after_compliance = _StubLibraryOutput(
        per_rule=(
            _StubRuleProjection(
                rule="sector_concentration_tech",
                status="WARNING",
                current=23.7,
                limit=25.0,
                projected_after=25.0,
                headroom_remaining=0.0,
                unit="% of portfolio (delta-adjusted)",
            ),
        ),
    )
    return library_output, after_compliance


def test_repeated_calls_produce_equal_payloads() -> None:
    library_output, after_compliance = _build_canonical_inputs()
    first = compose_hard_rejection_payload(
        rejected_command_id="PM.X.1.1",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="reduce size",
    )
    for _ in range(99):
        repeat = compose_hard_rejection_payload(
            rejected_command_id="PM.X.1.1",
            library_output=library_output,
            library_output_after_hypothetical_compliance=after_compliance,
            suggested_modification="reduce size",
        )
        assert repeat == first


def test_payload_is_frozen() -> None:
    library_output, after_compliance = _build_canonical_inputs()
    payload = compose_hard_rejection_payload(
        rejected_command_id="PM.X.1.1",
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification="reduce size",
    )
    with pytest.raises((ValueError, TypeError)):
        payload.suggested_modification = "tampered"


# ---------------------------------------------------------------------------
# Worked example (section 4 of the story spec)
# ---------------------------------------------------------------------------


_WORKED_LIBRARY_OUTPUT = _StubLibraryOutput(
    per_rule=(
        _StubRuleProjection(
            rule="position_max_size_pct",
            status="PASS",
            current=0.0,
            limit=5.0,
            projected_after=2.5,
            headroom_remaining=2.5,
            unit="% of portfolio",
        ),
        _StubRuleProjection(
            rule="sector_concentration_tech",
            status="FAIL",
            current=23.7,
            limit=25.0,
            projected_after=28.2,
            headroom_remaining=-3.2,
            unit="% of portfolio (delta-adjusted)",
        ),
        _StubRuleProjection(
            rule="net_long_pct",
            status="PASS",
            current=42.0,
            limit=60.0,
            projected_after=44.5,
            headroom_remaining=15.5,
            unit="% of portfolio (delta-adjusted)",
        ),
    ),
)
_WORKED_AFTER_COMPLIANCE = _StubLibraryOutput(
    per_rule=(
        _StubRuleProjection(
            rule="position_max_size_pct",
            status="PASS",
            current=0.0,
            limit=5.0,
            projected_after=1.4,
            headroom_remaining=3.6,
            unit="% of portfolio",
        ),
        _StubRuleProjection(
            rule="sector_concentration_tech",
            status="WARNING",
            current=23.7,
            limit=25.0,
            projected_after=25.0,
            headroom_remaining=0.0,
            unit="% of portfolio (delta-adjusted)",
        ),
        _StubRuleProjection(
            rule="net_long_pct",
            status="PASS",
            current=42.0,
            limit=60.0,
            projected_after=43.4,
            headroom_remaining=16.6,
            unit="% of portfolio (delta-adjusted)",
        ),
    ),
)
_WORKED_EXPECTED_BREACHING = (
    {
        "rule_id": "sector_concentration_tech",
        "current_value": 23.7,
        "limit_value": 25.0,
        "projected_after": 28.2,
        "overage": 3.2,
        "headroom_remaining": -3.2,
        "unit": "% of portfolio (delta-adjusted)",
    },
)
_WORKED_EXPECTED_POST_COMPLIANCE = (
    {
        "rule_id": "position_max_size_pct",
        "current_value": 0.0,
        "limit_value": 5.0,
        "projected_after": 1.4,
        "overage": 0.0,
        "headroom_remaining": 3.6,
        "unit": "% of portfolio",
    },
    {
        "rule_id": "sector_concentration_tech",
        "current_value": 23.7,
        "limit_value": 25.0,
        "projected_after": 25.0,
        "overage": 0.0,
        "headroom_remaining": 0.0,
        "unit": "% of portfolio (delta-adjusted)",
    },
    {
        "rule_id": "net_long_pct",
        "current_value": 42.0,
        "limit_value": 60.0,
        "projected_after": 43.4,
        "overage": 0.0,
        "headroom_remaining": 16.6,
        "unit": "% of portfolio (delta-adjusted)",
    },
)


def _assert_entry_matches(actual: RejectionRuleEntry, expected: dict[str, object]) -> None:
    assert actual.rule_id == expected["rule_id"]
    assert actual.unit == expected["unit"]
    for numeric_field in (
        "current_value",
        "limit_value",
        "projected_after",
        "overage",
        "headroom_remaining",
    ):
        assert getattr(actual, numeric_field) == pytest.approx(expected[numeric_field])


@pytest.mark.parametrize(
    (
        "library_output",
        "after_compliance",
        "rejected_command_id",
        "suggested_modification",
        "expected_breaching",
        "expected_post_compliance",
    ),
    [
        (
            _WORKED_LIBRARY_OUTPUT,
            _WORKED_AFTER_COMPLIANCE,
            "PM.20260428T100000Z.1.1",
            "reduce size by 42% (from 4.5% to 2.6% of portfolio)",
            _WORKED_EXPECTED_BREACHING,
            _WORKED_EXPECTED_POST_COMPLIANCE,
        ),
    ],
)
def test_worked_example_matches_documented_expected_output(
    library_output: _StubLibraryOutput,
    after_compliance: _StubLibraryOutput,
    rejected_command_id: str,
    suggested_modification: str,
    expected_breaching: tuple[dict[str, object], ...],
    expected_post_compliance: tuple[dict[str, object], ...],
) -> None:
    payload = compose_hard_rejection_payload(
        rejected_command_id=rejected_command_id,
        library_output=library_output,
        library_output_after_hypothetical_compliance=after_compliance,
        suggested_modification=suggested_modification,
    )
    assert payload.rejected_command_id == rejected_command_id
    assert payload.suggested_modification == suggested_modification
    assert len(payload.breaching_rules) == len(expected_breaching)
    for actual, expected in zip(payload.breaching_rules, expected_breaching, strict=True):
        _assert_entry_matches(actual, expected)
    assert len(payload.headroom_after_hypothetical_compliance) == len(expected_post_compliance)
    for actual, expected in zip(
        payload.headroom_after_hypothetical_compliance,
        expected_post_compliance,
        strict=True,
    ):
        _assert_entry_matches(actual, expected)
