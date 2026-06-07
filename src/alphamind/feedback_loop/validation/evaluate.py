"""Validation EVALUATE logic — the functional core (ALP-889 story 07b).

Two pure derivations the EVALUATE shell delegates to:

* :func:`derive_verdict` — classify the pre/post watched-metric movement against
  the registration's ``expected_direction`` into one of the four
  :class:`~alphamind.feedback_loop.validation.records.Verdict` values.
* :func:`derive_rollback_status` — apply the rollback evidence protocol table
  (``docs/design/feedback-loop.md`` § Rollback evidence protocol) to a verdict
  plus the operator-supplied confounder / failure-criterion judgments and the
  most-recent prior outcome on the same artifact.

Both are pure over plain values — no session, no I/O — so the protocol table is
testable per-branch without a DB. The DB-touching evaluation shell lives
elsewhere (it loads the windows, computes the metrics via the registry, and
calls these); it is added in a later cycle of this story.
"""

from __future__ import annotations

from enum import Enum, auto

from alphamind.feedback_loop.metrics.types import MetricResult
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    RollbackStatus,
    Verdict,
)


class _Movement(Enum):
    """Per-metric classification of post-vs-pre movement relative to expectation."""

    AS_EXPECTED = auto()
    AGAINST_EXPECTED = auto()
    INDISTINGUISHABLE = auto()
    UNREADABLE = auto()


def _expected_sign(direction: ExpectedDirection) -> int:
    """The sign of the metric movement the registration predicts.

    ``IMPROVED`` predicts an increase (+1), ``DEGRADED`` a decrease (-1).
    ``UNCHANGED`` predicts no directional movement (0).
    """
    if direction is ExpectedDirection.IMPROVED:
        return 1
    if direction is ExpectedDirection.DEGRADED:
        return -1
    return 0


def _classify_movement(
    pre: MetricResult | None,
    post: MetricResult | None,
    direction: ExpectedDirection,
) -> _Movement:
    """Classify one watched metric's movement against the expected direction.

    A reading is *unreadable* when either window is missing the metric, the post
    sample is insufficient, or either value is ``None`` (empty sample). When the
    post result carries a posterior band and the pre value falls inside it the
    movement is not distinguishable from noise (``INDISTINGUISHABLE``); a
    bandless metric only reads ``INDISTINGUISHABLE`` on an exact non-movement.
    """
    if pre is None or post is None or post.insufficient_sample:
        return _Movement.UNREADABLE
    if pre.value is None or post.value is None:
        return _Movement.UNREADABLE

    if post.posterior_band is not None:
        if post.posterior_band.lower <= pre.value <= post.posterior_band.upper:
            return _Movement.INDISTINGUISHABLE
    elif post.value == pre.value:
        return _Movement.INDISTINGUISHABLE

    delta = post.value - pre.value
    expected_sign = _expected_sign(direction)
    if expected_sign == 0:
        # The registration expected no movement; any distinguishable move is
        # against that expectation.
        return _Movement.AGAINST_EXPECTED
    moved_sign = 1 if delta > 0 else -1
    return _Movement.AS_EXPECTED if moved_sign == expected_sign else _Movement.AGAINST_EXPECTED


def derive_verdict(
    *,
    pre: tuple[MetricResult | None, ...],
    post: tuple[MetricResult | None, ...],
    expected_direction: ExpectedDirection,
    confounder_flagged: bool,
) -> Verdict:
    """Classify the watched-metric movement into a :class:`Verdict`.

    ``pre`` and ``post`` are positionally aligned watched-metric readings (a
    ``None`` entry is an unregistered / gated metric the registry could not
    resolve). The verdict is:

    * ``inconclusive`` — a confounder was flagged, or any watched metric is
      unreadable (missing, empty, or insufficient sample). Attribution is
      impossible, so the honest reading is "we don't know".
    * ``degraded`` — at least one metric moved against the expected direction.
    * ``improved`` — at least one metric moved as expected and none moved against.
    * ``no_change`` — no metric's movement is distinguishable from noise.
    """
    if confounder_flagged:
        return Verdict.INCONCLUSIVE

    movements = tuple(
        _classify_movement(p, q, expected_direction)
        for p, q in zip(pre, post, strict=True)
    )
    if any(m is _Movement.UNREADABLE for m in movements):
        return Verdict.INCONCLUSIVE
    if any(m is _Movement.AGAINST_EXPECTED for m in movements):
        return Verdict.DEGRADED
    if any(m is _Movement.AS_EXPECTED for m in movements):
        return Verdict.IMPROVED
    return Verdict.NO_CHANGE


def derive_rollback_status(
    *,
    verdict: Verdict,
    failure_criterion_crossed: bool,
    confounder_flagged: bool,
    expected_direction: ExpectedDirection,
    prior_verdict: Verdict | None,
) -> RollbackStatus:
    """Apply the rollback evidence protocol table to a verdict and its context.

    Per ``docs/design/feedback-loop.md`` § Rollback evidence protocol:

    * ``mandatory_clean_failure`` — verdict ``degraded``, the post-edit window
      crossed the pre-registered failure criterion, and no confounder was flagged.
    * ``optional_pending_retrospective`` — verdict ``degraded`` with a confounder
      flagged, OR ``no_change`` when the registration expected ``improved``, OR
      ``inconclusive`` when the most-recent prior outcome on the same
      ``edited_artifact`` (``prior_verdict``) was also ``inconclusive``.
    * ``not_applicable`` — any verdict shape not covered above.

    The derivation is mechanical: it introduces no judgment beyond the
    pre-registered failure criterion and the confounder check supplied by the
    caller.
    """
    if verdict is Verdict.DEGRADED:
        if failure_criterion_crossed and not confounder_flagged:
            return RollbackStatus.MANDATORY_CLEAN_FAILURE
        if confounder_flagged:
            return RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE
        return RollbackStatus.NOT_APPLICABLE
    if verdict is Verdict.NO_CHANGE and expected_direction is ExpectedDirection.IMPROVED:
        return RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE
    if verdict is Verdict.INCONCLUSIVE and prior_verdict is Verdict.INCONCLUSIVE:
        return RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE
    return RollbackStatus.NOT_APPLICABLE


__all__ = [
    "derive_rollback_status",
    "derive_verdict",
]
