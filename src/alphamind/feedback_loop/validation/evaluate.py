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

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import TYPE_CHECKING

from alphamind.feedback_loop.dataset import WindowDataset, load_window
from alphamind.feedback_loop.metrics import get_metric
from alphamind.feedback_loop.metrics.types import UNCONDITIONED, MetricResult
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    OutcomeId,
    RollbackStatus,
    SupersededReason,
    ValidationId,
    ValidationOutcomeRecord,
    Verdict,
)
from alphamind.state.repository.validation_queries import (
    insert_validation_outcome,
    read_outcomes_by_artifact,
    read_validation,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

    from alphamind.config.models.feedback import FeedbackLoopConfig
    from alphamind.feedback_loop.validation.records import ValidationRecord


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
        _classify_movement(p, q, expected_direction) for p, q in zip(pre, post, strict=True)
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


# ---------------------------------------------------------------------------
# EVALUATE shell — the one DB-touching surface of the validation logic
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationJudgments:
    """The operator/skill determinations the verdict + rollback derivation needs.

    These are judgments the free-text success/failure criteria cannot yield
    mechanically — the EVALUATE walk surfaces them and the skill (or the headless
    CLI) supplies them explicitly. Kept as required, defaultless fields so the
    dependency on operator judgment is visible at every call site rather than
    silently defaulted.

    * ``confounder_flagged`` — a residual confounder (regime distribution
      mismatch, model-version straddle, concurrent edit) was flagged on the
      comparison; forces the verdict to ``inconclusive`` and downgrades any
      rollback obligation.
    * ``failure_criterion_crossed`` — the post-edit window crossed the
      pre-registered failure criterion (the operator's own contract firing).
    * ``confounder_notes`` — free-text detail persisted on the outcome.
    """

    confounder_flagged: bool
    failure_criterion_crossed: bool
    confounder_notes: str | None


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """The outcome of an evaluation attempt.

    ``outcome`` is the written record on a real evaluation and ``None`` when the
    validation was superseded (in which case ``superseded`` is ``True`` and
    ``superseded_reason`` carries why) — superseded validations produce no
    outcome record per the validation methodology.
    """

    outcome: ValidationOutcomeRecord | None
    superseded: bool
    superseded_reason: SupersededReason | None


def _compute_window_results(
    dataset: WindowDataset,
    watched_metric_ids: tuple[MetricId, ...],
) -> tuple[MetricResult | None, ...]:
    """Compute each watched metric's whole-window reading over *dataset*.

    A watched metric the registry cannot resolve (gated / unregistered) maps to
    ``None`` (graceful degradation), positionally aligned with
    ``watched_metric_ids`` so the verdict derivation reads it as unreadable.
    """
    results: list[MetricResult | None] = []
    for metric_id in watched_metric_ids:
        metric = get_metric(metric_id)
        if metric is None:
            results.append(None)
            continue
        results.append(metric.compute(dataset, UNCONDITIONED))
    return tuple(results)


def _result_summary(result: MetricResult | None) -> dict[str, object]:
    """Render one ``MetricResult`` (or its absence) into a JSON-safe mapping."""
    if result is None:
        return {"available": False}
    band = result.posterior_band
    return {
        "available": True,
        "value": result.value,
        "sample_size": result.sample_size,
        "insufficient_sample": result.insufficient_sample,
        "posterior_band": None if band is None else [band.lower, band.upper],
    }


def _posterior_summary(
    watched_metric_ids: tuple[MetricId, ...],
    pre: tuple[MetricResult | None, ...],
    post: tuple[MetricResult | None, ...],
) -> dict[str, object]:
    """Build the per-metric pre/post posterior summary persisted on the outcome."""
    return {
        str(metric_id): {
            "pre": _result_summary(pre_result),
            "post": _result_summary(post_result),
        }
        for metric_id, pre_result, post_result in zip(watched_metric_ids, pre, post, strict=True)
    }


def _prior_verdict(session: Session, edited_artifact: str) -> Verdict | None:
    """The most-recent prior outcome's verdict on *edited_artifact*, or ``None``."""
    outcomes = read_outcomes_by_artifact(session, edited_artifact)
    if not outcomes:
        return None
    return outcomes[0].verdict


async def evaluate_validation(
    session: AsyncSession,
    *,
    validation_id: ValidationId,
    outcome_id: OutcomeId,
    evaluated_at: datetime,
    judgments: EvaluationJudgments,
    narrative: str,
    evaluated_by_session_id: str | None = None,
    config: FeedbackLoopConfig | None = None,
) -> EvaluationResult:
    """Evaluate a registered validation and write its outcome.

    Loads the pre-edit and post-edit windows via ``load_window``, computes the
    watched metrics' ``MetricResult``s through the registry, derives the verdict
    and ``rollback_status``, and writes the ``validation_outcomes`` record. The
    outcome is queued on *session*; the caller owns the commit boundary.

    A superseded validation short-circuits: no windows are loaded, no outcome is
    written, and the supersession state is returned. Raises :class:`KeyError`
    when no validation with *validation_id* exists.

    The pre-edit window is the ``window_length_days`` span immediately before
    ``registered_at``; the post-edit window is the span from ``registered_at`` to
    ``evaluation_due_at`` (the registered window). Both use ``[start, end)``.
    """
    validation = await session.run_sync(
        lambda sync_session: read_validation(sync_session, validation_id)
    )
    if validation is None:
        msg = f"no validation found for id={validation_id!r}"
        raise KeyError(msg)
    if validation.superseded_at is not None:
        return EvaluationResult(
            outcome=None,
            superseded=True,
            superseded_reason=validation.superseded_reason,
        )

    pre, post = await _load_pre_post(session, validation, config)
    pre_results = _compute_window_results(pre, validation.watched_metric_ids)
    post_results = _compute_window_results(post, validation.watched_metric_ids)

    verdict = derive_verdict(
        pre=pre_results,
        post=post_results,
        expected_direction=validation.expected_direction,
        confounder_flagged=judgments.confounder_flagged,
    )
    prior_verdict = await session.run_sync(
        lambda sync_session: _prior_verdict(sync_session, validation.edited_artifact)
    )
    rollback_status = derive_rollback_status(
        verdict=verdict,
        failure_criterion_crossed=judgments.failure_criterion_crossed,
        confounder_flagged=judgments.confounder_flagged,
        expected_direction=validation.expected_direction,
        prior_verdict=prior_verdict,
    )

    outcome = ValidationOutcomeRecord(
        outcome_id=outcome_id,
        validation_id=validation_id,
        evaluated_at=evaluated_at,
        evaluated_by_session_id=evaluated_by_session_id,
        verdict=verdict,
        posterior_summary=_posterior_summary(
            validation.watched_metric_ids, pre_results, post_results
        ),
        confounder_notes=judgments.confounder_notes,
        narrative=narrative,
        rollback_status=rollback_status,
    )
    await session.run_sync(lambda sync_session: insert_validation_outcome(sync_session, outcome))
    return EvaluationResult(outcome=outcome, superseded=False, superseded_reason=None)


async def _load_pre_post(
    session: AsyncSession,
    validation: ValidationRecord,
    config: FeedbackLoopConfig | None,
) -> tuple[WindowDataset, WindowDataset]:
    """Load the pre-edit and post-edit ``WindowDataset``s for *validation*."""
    window = timedelta(days=validation.window_length_days)
    pre = await load_window(
        session,
        validation.registered_at - window,
        validation.registered_at,
        config,
    )
    post = await load_window(
        session,
        validation.registered_at,
        validation.evaluation_due_at,
        config,
    )
    return pre, post


__all__ = [
    "EvaluationJudgments",
    "EvaluationResult",
    "derive_rollback_status",
    "derive_verdict",
    "evaluate_validation",
]
