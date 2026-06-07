"""Pure verdict + rollback-status derivation for the validation EVALUATE walk (ALP-889).

These are the functional-core derivations the EVALUATE shell delegates to: no
session, no I/O. The rollback-status protocol table
(``docs/design/feedback-loop.md`` § Rollback evidence protocol) is exercised
per-branch here, where it needs no DB.
"""

from __future__ import annotations

from alphamind.feedback_loop.metrics.types import MetricId, MetricResult, PosteriorBand
from alphamind.feedback_loop.validation.evaluate import (
    derive_rollback_status,
    derive_verdict,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    RollbackStatus,
    Verdict,
)


def _result(
    value: float | None,
    *,
    sample_size: int = 50,
    insufficient: bool = False,
    band: PosteriorBand | None = None,
) -> MetricResult:
    return MetricResult(
        metric_id=MetricId("m"),
        value=value,
        posterior_band=band,
        sample_size=sample_size,
        insufficient_sample=insufficient,
    )


class TestDeriveRollbackStatus:
    def test_mandatory_clean_failure(self) -> None:
        status = derive_rollback_status(
            verdict=Verdict.DEGRADED,
            failure_criterion_crossed=True,
            confounder_flagged=False,
            expected_direction=ExpectedDirection.IMPROVED,
            prior_verdict=None,
        )
        assert status is RollbackStatus.MANDATORY_CLEAN_FAILURE

    def test_optional_degraded_with_confounder(self) -> None:
        status = derive_rollback_status(
            verdict=Verdict.DEGRADED,
            failure_criterion_crossed=True,
            confounder_flagged=True,
            expected_direction=ExpectedDirection.IMPROVED,
            prior_verdict=None,
        )
        assert status is RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE

    def test_optional_no_change_when_expected_improved(self) -> None:
        status = derive_rollback_status(
            verdict=Verdict.NO_CHANGE,
            failure_criterion_crossed=False,
            confounder_flagged=False,
            expected_direction=ExpectedDirection.IMPROVED,
            prior_verdict=None,
        )
        assert status is RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE

    def test_optional_inconclusive_after_prior_inconclusive(self) -> None:
        status = derive_rollback_status(
            verdict=Verdict.INCONCLUSIVE,
            failure_criterion_crossed=False,
            confounder_flagged=False,
            expected_direction=ExpectedDirection.IMPROVED,
            prior_verdict=Verdict.INCONCLUSIVE,
        )
        assert status is RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE

    def test_not_applicable_improved(self) -> None:
        status = derive_rollback_status(
            verdict=Verdict.IMPROVED,
            failure_criterion_crossed=False,
            confounder_flagged=False,
            expected_direction=ExpectedDirection.IMPROVED,
            prior_verdict=None,
        )
        assert status is RollbackStatus.NOT_APPLICABLE

    def test_not_applicable_degraded_without_criterion_crossing(self) -> None:
        """Degraded but the pre-registered failure criterion was not crossed,
        no confounder — falls through to ``not_applicable``."""
        status = derive_rollback_status(
            verdict=Verdict.DEGRADED,
            failure_criterion_crossed=False,
            confounder_flagged=False,
            expected_direction=ExpectedDirection.IMPROVED,
            prior_verdict=None,
        )
        assert status is RollbackStatus.NOT_APPLICABLE

    def test_not_applicable_no_change_when_not_expecting_improved(self) -> None:
        status = derive_rollback_status(
            verdict=Verdict.NO_CHANGE,
            failure_criterion_crossed=False,
            confounder_flagged=False,
            expected_direction=ExpectedDirection.UNCHANGED,
            prior_verdict=None,
        )
        assert status is RollbackStatus.NOT_APPLICABLE

    def test_not_applicable_inconclusive_without_prior_inconclusive(self) -> None:
        status = derive_rollback_status(
            verdict=Verdict.INCONCLUSIVE,
            failure_criterion_crossed=False,
            confounder_flagged=False,
            expected_direction=ExpectedDirection.IMPROVED,
            prior_verdict=Verdict.IMPROVED,
        )
        assert status is RollbackStatus.NOT_APPLICABLE


class TestDeriveVerdict:
    def test_inconclusive_when_any_post_sample_insufficient(self) -> None:
        verdict = derive_verdict(
            pre=(_result(0.5),),
            post=(_result(0.6, insufficient=True),),
            expected_direction=ExpectedDirection.IMPROVED,
            confounder_flagged=False,
        )
        assert verdict is Verdict.INCONCLUSIVE

    def test_inconclusive_when_confounder_flagged(self) -> None:
        verdict = derive_verdict(
            pre=(_result(0.5),),
            post=(_result(0.6),),
            expected_direction=ExpectedDirection.IMPROVED,
            confounder_flagged=True,
        )
        assert verdict is Verdict.INCONCLUSIVE

    def test_improved_when_movement_matches_expected_up(self) -> None:
        verdict = derive_verdict(
            pre=(_result(0.40),),
            post=(_result(0.60),),
            expected_direction=ExpectedDirection.IMPROVED,
            confounder_flagged=False,
        )
        assert verdict is Verdict.IMPROVED

    def test_degraded_when_movement_opposes_expected_up(self) -> None:
        verdict = derive_verdict(
            pre=(_result(0.60),),
            post=(_result(0.40),),
            expected_direction=ExpectedDirection.IMPROVED,
            confounder_flagged=False,
        )
        assert verdict is Verdict.DEGRADED

    def test_no_change_when_pre_falls_within_post_band(self) -> None:
        """Band-aware: when the pre value sits inside the post posterior band the
        movement is not distinguishable from noise — ``no_change``, not improved."""
        verdict = derive_verdict(
            pre=(_result(0.50),),
            post=(_result(0.55, band=PosteriorBand(lower=0.45, upper=0.65)),),
            expected_direction=ExpectedDirection.IMPROVED,
            confounder_flagged=False,
        )
        assert verdict is Verdict.NO_CHANGE

    def test_no_change_when_bandless_values_equal(self) -> None:
        """A bandless (process) metric has no uncertainty model, so only an exact
        non-movement reads as ``no_change``."""
        verdict = derive_verdict(
            pre=(_result(0.50),),
            post=(_result(0.50),),
            expected_direction=ExpectedDirection.IMPROVED,
            confounder_flagged=False,
        )
        assert verdict is Verdict.NO_CHANGE

    def test_inconclusive_when_a_watched_metric_is_missing(self) -> None:
        """A ``None`` post result (metric unregistered / gated) is unreadable."""
        verdict = derive_verdict(
            pre=(_result(0.5),),
            post=(None,),
            expected_direction=ExpectedDirection.IMPROVED,
            confounder_flagged=False,
        )
        assert verdict is Verdict.INCONCLUSIVE
