"""WindowDataset + loader shell for the analytics spine (ALP-882 story 05).

The **imperative shell** of the functional-core / imperative-shell split (P1): the one
DB-touching function in the analytics layer is :func:`load_window`. It composes the
existing repository read-helpers into a typed, in-memory, load-once
:class:`WindowDataset` that the metric cores (``feedback_loop.metrics``) compute over
**purely** — no metric ever touches the session.

The dataset is a frozen dataclass composed of per-domain sub-bundles. Two extension
sub-bundles are **pre-declared empty** here so later stories fill distinct, non-colliding
regions of this module:

* ``refs`` — citation reference IDs parsed from agent-call output artifacts. Filled by
  story 06d via the :func:`_load_refs` hook (a stub returning empty here).
* ``replays`` — counterfactual-replay records. Filled by story 06e via the
  :func:`_load_replays` hook (a stub returning empty here, gated on ALP-129).

The ``outcomes`` sub-bundle (story 06c) carries the resolved-thesis outcome
observations the outcome-tier metrics calibrate against, each paired with its
conditioning attributes (regime / sector / conviction / strategist status /
anti-patterns / time-of-day / prompt version / model version) so the conditioning
surface can slice without the typed thesis record carrying those dimensions.

``feedback_loop`` is read-only over trading state: this module only reads, and never
imports ``execution`` (enforced by ``.importlinter``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from alphamind.portfolio_state.records.theses import ThesisRecord
from alphamind.state.repository.activity_log_queries import read_recent_pm_decision_log
from alphamind.state.repository.agent_calls_queries import read_agent_calls_in_window
from alphamind.state.repository.outcome_queries import read_resolved_theses_in_window
from alphamind.state.repository.validation_queries import read_pending_validations

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from alphamind._kernel.ids import PositionId, ThesisId
    from alphamind.config.models.feedback import FeedbackLoopConfig
    from alphamind.feedback_loop.validation.records import ValidationRecord
    from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
    from alphamind.portfolio_state.records.theses import ThesisResolutionCategory
    from alphamind.state.tables.agent_calls import AgentCallRecord

# ---------------------------------------------------------------------------
# Loader configuration
# ---------------------------------------------------------------------------

#: Size of the PM-decision sliding window the loader pulls for the dataset
#: (``read_recent_pm_decision_log``'s ``sliding_window_invocations``). The
#: analytics layer reads the most-recent decisions adjacent to the window; this
#: is a definitional read-depth, not an operator knob.
_PM_DECISION_SLIDING_WINDOW = 50

#: Seconds-per-hour divisor for rendering a resolved thesis's active duration in
#: hours (resolution_timestamp minus generation_timestamp). Definitional unit
#: conversion, not a tunable.
_SECONDS_PER_HOUR = 3600.0


# ---------------------------------------------------------------------------
# Per-domain sub-bundles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefsBundle:
    """Citation reference IDs and their chains (story 06d fills this).

    Pre-declared empty so the citation-chain metrics can build against the shape
    without 06d and this story colliding on ``dataset.py``.
    """

    citations: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplaysBundle:
    """Counterfactual-replay records for the window (story 06e fills this).

    Pre-declared empty and gated on ALP-129; the PM-accuracy / modification-
    effectiveness metrics read it. Absent until 06e wires the loader hook.
    """

    replays: tuple[object, ...] = ()


# ---------------------------------------------------------------------------
# Outcomes sub-bundle (story 06c — outcome + calibration metrics)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConditioningAttributes:
    """The conditioning-surface dimensions attached to one outcome observation.

    Each field is the value of a :class:`~alphamind.feedback_loop.metrics.types.\
ConditioningDimension` for this resolved thesis — the provenance the conditioning
    surface slices on. They are carried *alongside* the resolution facts (not on the
    typed thesis record, which does not know about analyst conviction / strategist
    status / invocation provenance) so a metric can filter to one slice purely.

    Every dimension is ``None`` / empty until story 04e wires the join from a
    resolved thesis back to its analyst conviction, strategist status, and
    invocation provenance. Fixtures populate them so the conditioning surface is
    fully exercised before that join lands. ``anti_patterns`` is a tuple because a
    position may carry several tagged patterns at once.
    """

    regime: str | None = None
    sector: str | None = None
    conviction: str | None = None
    strategist_status: str | None = None
    anti_patterns: tuple[str, ...] = ()
    time_of_day: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None


@dataclass(frozen=True, slots=True)
class ThesisOutcome:
    """One resolved-thesis outcome observation the outcome-tier metrics score.

    Pairs the realized resolution facts (category, P/L, realized vs expected
    duration) with the :class:`ConditioningAttributes` provenance. The metrics read
    these fields only — never the full typed thesis record — so the conditioning
    surface and the outcome surface compute over a single flat observation.
    """

    thesis_id: ThesisId
    position_id: PositionId
    resolution_category: ThesisResolutionCategory
    resolution_pnl_usd: float
    active_duration_hours: float
    expected_duration_hours: float
    conditioning: ConditioningAttributes


@dataclass(frozen=True, slots=True)
class OutcomesBundle:
    """Resolved-thesis outcome observations + the sample-size thresholds (story 06c).

    ``theses`` are the resolved-thesis observations in the window. The two
    ``min_resolved_theses_*`` thresholds are the operator-tunable
    :class:`~alphamind.config.models.feedback.FeedbackLoopConfig` floors, stamped
    onto the dataset by :func:`load_window` so the *pure* metric cores see the
    insufficient-sample threshold without taking config as a ``compute`` argument
    (functional core / imperative shell). Defaults match the packaged
    ``config/feedback.yaml`` so a hand-built dataset omitting config is still
    well-formed for tests.
    """

    theses: tuple[ThesisOutcome, ...] = ()
    min_resolved_theses_monthly: int = 30
    min_resolved_theses_quarterly: int = 60


# ---------------------------------------------------------------------------
# WindowDataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowDataset:
    """Typed, in-memory, load-once view of every record a window's metrics need.

    Built once by :func:`load_window`; metric cores read it without further I/O.

    **Per-bundle window semantics differ — read carefully when computing windowed
    metrics (``start`` inclusive, ``end`` exclusive):**

    * ``agent_calls`` — strictly bounded to ``[start, end)`` (via the invocation join).
    * ``pm_decision_log`` — the most-recent ``_PM_DECISION_SLIDING_WINDOW`` decisions
      (a count-based read-depth), **not** clipped to ``[start, end)``: it may include
      decisions outside the window or omit in-window decisions beyond that depth.
    * ``validations`` — all currently-pending validations (point-in-time), **not**
      window-bounded.

    A metric needing a strict per-window slice of ``pm_decision_log`` / ``validations``
    must filter by timestamp itself.
    """

    start: datetime
    end: datetime
    agent_calls: tuple[AgentCallRecord, ...]
    pm_decision_log: tuple[ActivityLogEntry, ...]
    validations: tuple[ValidationRecord, ...]
    # Pre-declared extension sub-bundles — empty until 06d / 06e fill them.
    refs: RefsBundle = field(default_factory=RefsBundle)
    replays: ReplaysBundle = field(default_factory=ReplaysBundle)
    # Resolved-thesis outcome observations + sample-size thresholds (story 06c).
    outcomes: OutcomesBundle = field(default_factory=OutcomesBundle)


# ---------------------------------------------------------------------------
# Extension loader hooks (pre-declared seams; stubbed empty this story)
# ---------------------------------------------------------------------------


async def _load_refs(
    session: AsyncSession,  # noqa: ARG001 — pre-declared seam; story 06d uses it
    start: datetime,  # noqa: ARG001 — pre-declared seam; story 06d uses it
    end: datetime,  # noqa: ARG001 — pre-declared seam; story 06d uses it
) -> RefsBundle:
    """Citation-reference sub-bundle loader hook (story 06d fills this).

    Stubbed to return an empty :class:`RefsBundle`. Story 06d parses agent-call
    output artifacts here; the seam is pre-declared so that work edits only this
    function and :class:`RefsBundle`, not the rest of the loader.
    """
    return RefsBundle()


async def _load_replays(
    session: AsyncSession,  # noqa: ARG001 — pre-declared seam; story 06e uses it
    start: datetime,  # noqa: ARG001 — pre-declared seam; story 06e uses it
    end: datetime,  # noqa: ARG001 — pre-declared seam; story 06e uses it
) -> ReplaysBundle:
    """Counterfactual-replay sub-bundle loader hook (story 06e fills this).

    Stubbed to return an empty :class:`ReplaysBundle`. Story 06e reads
    ``counterfactual_replays`` here (gated on ALP-129); the seam is pre-declared
    so that work edits only this function and :class:`ReplaysBundle`.
    """
    return ReplaysBundle()


async def _load_outcomes(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    config: FeedbackLoopConfig | None,
) -> OutcomesBundle:
    """Resolved-thesis outcome sub-bundle loader hook (story 06c).

    Reads the RESOLVED theses resolved in ``[start, end)`` and projects each into a
    :class:`ThesisOutcome`. Conditioning attributes are left empty here — the join
    from a resolved thesis back to its analyst conviction / strategist status /
    invocation provenance lands with story 04e; until then the conditioning surface
    is exercised by fixtures. When *config* is supplied its sample-size thresholds
    are stamped onto the bundle; otherwise the packaged defaults stand.
    """
    resolved = await read_resolved_theses_in_window(session, start, end)
    theses = tuple(_thesis_to_outcome(record) for record in resolved)
    if config is None:
        return OutcomesBundle(theses=theses)
    return OutcomesBundle(
        theses=theses,
        min_resolved_theses_monthly=config.min_resolved_theses_monthly,
        min_resolved_theses_quarterly=config.min_resolved_theses_quarterly,
    )


def _thesis_to_outcome(record: ThesisRecord) -> ThesisOutcome:
    """Project a resolved ``ThesisRecord`` into a flat :class:`ThesisOutcome`.

    The realized resolution facts are guaranteed non-``None`` for a RESOLVED record
    by ``ThesisRecord``'s own validators. Conditioning attributes are empty until
    story 04e wires the provenance join.
    """
    if record.resolution_category is None or record.resolution_pnl_usd is None:
        msg = f"resolved thesis {record.thesis_id!r} missing realized resolution facts"
        raise ValueError(msg)
    if record.resolution_timestamp is None:
        msg = f"resolved thesis {record.thesis_id!r} missing resolution_timestamp"
        raise ValueError(msg)
    active_duration_hours = (
        record.resolution_timestamp - record.generation_timestamp
    ).total_seconds() / _SECONDS_PER_HOUR
    return ThesisOutcome(
        thesis_id=record.thesis_id,
        position_id=record.position_id,
        resolution_category=record.resolution_category,
        resolution_pnl_usd=record.resolution_pnl_usd,
        active_duration_hours=active_duration_hours,
        expected_duration_hours=record.time_expectation_hours,
        conditioning=ConditioningAttributes(),
    )


# ---------------------------------------------------------------------------
# The loader — the only DB-touching function in the analytics layer
# ---------------------------------------------------------------------------


async def load_window(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    config: FeedbackLoopConfig | None = None,
) -> WindowDataset:
    """Compose the repository read-helpers into a :class:`WindowDataset`.

    Reads every record the window's metrics need in one pass:

    * ``agent_calls`` whose owning invocation started in ``[start, end)``.
    * the recent PM-decision activity-log sliding window.
    * pending validation contracts (the validation-discipline read surface).
    * the resolved-thesis ``outcomes`` sub-bundle, stamped with *config*'s
      sample-size thresholds (the packaged defaults when *config* is omitted).
    * the ``refs`` / ``replays`` extension sub-bundles via their hooks (empty
      until 06d / 06e fill them).

    ``validation_queries`` exposes synchronous ``Session``-based helpers; they are
    bridged onto this async session via :meth:`AsyncSession.run_sync` — SQLAlchemy
    2.x's canonical sync bridge over the same connection.
    """
    agent_calls = await read_agent_calls_in_window(session, start, end)
    pm_decision_log = await read_recent_pm_decision_log(session, _PM_DECISION_SLIDING_WINDOW)
    validations = await session.run_sync(
        lambda sync_session: read_pending_validations(sync_session)
    )
    refs = await _load_refs(session, start, end)
    replays = await _load_replays(session, start, end)
    outcomes = await _load_outcomes(session, start, end, config)
    return WindowDataset(
        start=start,
        end=end,
        agent_calls=agent_calls,
        pm_decision_log=pm_decision_log,
        validations=validations,
        refs=refs,
        replays=replays,
        outcomes=outcomes,
    )


__all__ = [
    "ConditioningAttributes",
    "OutcomesBundle",
    "RefsBundle",
    "ReplaysBundle",
    "ThesisOutcome",
    "WindowDataset",
    "load_window",
]
