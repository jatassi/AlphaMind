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

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import yaml
from sqlalchemy import select

from alphamind.config.models.agents import AgentName, AgentsConfig
from alphamind.feedback_loop.citation.parser import (
    ComponentCitation,
    assemble_chains,
    extract_payload_citations,
)
from alphamind.portfolio_state.events.types import EventType
from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisRecord,
)
from alphamind.state.repository.activity_log_queries import (
    read_activity_events_in_window,
    read_recent_pm_decision_log,
)
from alphamind.state.repository.agent_calls_queries import (
    read_agent_calls_for_agent,
    read_agent_calls_in_window,
)
from alphamind.state.repository.invocation_queries import (
    read_invocation_regimes_in_window,
)
from alphamind.state.repository.outcome_queries import (
    InvocationConditioning,
    read_invocation_conditioning,
    read_resolved_theses_in_window,
)
from alphamind.state.repository.validation_queries import (
    read_pending_validations,
    read_validations_superseded_in_window,
)
from alphamind.state.tables.thesis_components import ThesisComponentRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

    from alphamind._kernel.ids import PositionId, ThesisId
    from alphamind.config.models.feedback import FeedbackLoopConfig
    from alphamind.feedback_loop.citation.parser import RefChain
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
    """Citation reference IDs and their cross-layer chains (story 06d / ALP-886).

    One :class:`~alphamind.feedback_loop.citation.parser.RefChain` per upstream
    reference available in the window — its source, whether it was cited in the
    synthesizer brief and in a decision-layer narrative, and the resolution of
    the thesis component it ended in. The citation-chain metrics
    (:mod:`alphamind.feedback_loop.citation.chain`) compute purely over this.
    """

    citations: tuple[RefChain, ...] = ()


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
# Cost / execution-process sub-bundles (story 06b — ALP-884)
#
# A distinct region from the citation (_load_refs) / window (load_window) seams so
# the concurrent 06g edits to _load_refs/load_window never collide here.
# ---------------------------------------------------------------------------

#: The packaged ``config/agents.yaml`` — the single source of truth for each agent's
#: latency budget envelope. Resolved by walking up: dataset.py → feedback_loop →
#: alphamind → src → repo root (mirrors ``analysis/synthesizer/runner._AGENTS_YAML``).
_AGENTS_YAML = Path(__file__).resolve().parents[3] / "config" / "agents.yaml"


@lru_cache(maxsize=1)
def _load_agent_latency_budgets() -> Mapping[str, int]:
    """Parse + validate the packaged ``agents.yaml`` once per process (memoized).

    The disk read and :class:`AgentsConfig` validation are pure and the packaged file is
    immutable for the process's lifetime, so the result is cached. Returns an immutable
    :class:`~types.MappingProxyType` snapshot so the cached object can never be mutated
    by a caller; :func:`_packaged_agent_latency_budgets` hands out fresh ``dict`` copies.
    """
    with _AGENTS_YAML.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    cfg = AgentsConfig.model_validate(data)
    return MappingProxyType(
        {name.value: entry.latency_budget_seconds for name, entry in cfg.agents.items()}
    )


def _packaged_agent_latency_budgets() -> dict[str, int]:
    """Per-agent ``latency_budget_seconds`` read from the packaged ``agents.yaml``.

    The budget the latency-headroom metric compares wall-clock against is config-sourced,
    not a hard-coded literal. Stamped onto :class:`CostBudgetsBundle` as the default
    factory so a hand-built test dataset omitting the loader still carries the real
    envelope. The disk-read + validate is memoized by :func:`_load_agent_latency_budgets`
    (``load_window`` builds a fresh bundle per call — 12x per default digest run);
    a *fresh* ``dict`` copy is returned per call so the frozen bundle instances never
    alias one shared mutable mapping.
    """
    return dict(_load_agent_latency_budgets())


@dataclass(frozen=True, slots=True)
class CostBudgetsBundle:
    """Per-agent budget envelopes the cost metrics compare observed telemetry against.

    Currently the per-agent ``latency_budget_seconds`` from ``config/agents.yaml`` (the
    latency-budget-headroom metric's denominator). Stamped onto the dataset by the loader
    so the *pure* cost metric cores read the envelope without taking config as a
    ``compute`` argument (functional core / imperative shell). The default reads the
    packaged ``agents.yaml`` so a hand-built dataset omitting config is well-formed.
    """

    agent_latency_budget_seconds: Mapping[str, int] = field(
        default_factory=_packaged_agent_latency_budgets
    )


@dataclass(frozen=True, slots=True)
class ActivityEventsBundle:
    """Window-bounded activity-log entries the execution-process metrics count.

    The ``pm_decision_log`` bundle is ``PM_DECISION``-only; the guardrail-rejection,
    command-abandonment, and engine-originated-CLOSE metrics count *other* event types
    (``GUARDRAIL_REJECTION`` / ``COMMAND_ABANDONED`` / monitor-direct ``POSITION_CLOSED``)
    over the window, so they read this separate strictly-``[start, end)`` slice rather
    than the count-based PM-decision sliding window. Empty until the loader fills it.
    """

    entries: tuple[ActivityLogEntry, ...] = ()


# ---------------------------------------------------------------------------
# Decision-layer sub-bundles deferred from 06a (story 06f — ALP-911)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalystProposalObservation:
    """One analyst invocation's proposal count — the analyst-metric observation.

    ``proposal_count`` is the number of ``recommendations`` a normal-mode analyst
    run emitted, or ``0`` for a watchlist-mode run (a deliberate no-new-entry
    invocation — an inaction). One observation per analyst *invocation* (retries
    deduped), so the analyst process metrics rate over invocations, not calls.
    """

    invocation_id: str
    proposal_count: int


@dataclass(frozen=True, slots=True)
class AnalystProposalsBundle:
    """Per-invocation analyst proposal observations the analyst metrics rate over.

    Populated by :func:`load_window` from each successful analyst ``agent_calls``
    run's ``output.json`` (``AnalystOutput``); empty until the loader fills it. The
    analyst process-metric cores (``analyst_inaction_rate`` /
    ``analyst_proposals_per_invocation``) compute purely over ``observations``.
    """

    observations: tuple[AnalystProposalObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class RegimeBundle:
    """Per-invocation ``active_regime`` map — the REGIME conditioning source.

    ``by_invocation`` maps each in-window invocation id to its
    ``invocations.active_regime`` value, so the decision metrics' ``REGIME``
    conditioning slice resolves an invocation's held regime without the
    ``pm_decision_log`` / analyst observation carrying the dimension. Empty until
    the loader fills it; an absent invocation simply does not match any slice.
    """

    by_invocation: Mapping[str, str] = field(default_factory=dict)


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
    * ``superseded_validations`` — validations whose ``superseded_at`` falls in
      ``[start, end)`` (the only window-bounded validation view): the supersessions
      that landed during the window, for the ``validation_superseded`` shift detector.

    A metric needing a strict per-window slice of ``pm_decision_log`` / ``validations``
    must filter by timestamp itself.
    """

    start: datetime
    end: datetime
    agent_calls: tuple[AgentCallRecord, ...]
    pm_decision_log: tuple[ActivityLogEntry, ...]
    validations: tuple[ValidationRecord, ...]
    superseded_validations: tuple[ValidationRecord, ...] = ()
    # Pre-declared extension sub-bundles — empty until 06d / 06e fill them.
    refs: RefsBundle = field(default_factory=RefsBundle)
    replays: ReplaysBundle = field(default_factory=ReplaysBundle)
    # Resolved-thesis outcome observations + sample-size thresholds (story 06c).
    outcomes: OutcomesBundle = field(default_factory=OutcomesBundle)
    # Cost / execution-process sub-bundles (story 06b). ``budgets`` carries the
    # agents.yaml latency envelope; ``activity_events`` the window-bounded non-PM
    # activity-log slice the execution-process metrics count.
    budgets: CostBudgetsBundle = field(default_factory=CostBudgetsBundle)
    activity_events: ActivityEventsBundle = field(default_factory=ActivityEventsBundle)
    # Decision-layer sub-bundles deferred from 06a (story 06f). ``analyst_proposals``
    # carries one proposal-count observation per analyst invocation; ``regimes`` the
    # per-invocation active-regime map the REGIME conditioning slice resolves through.
    analyst_proposals: AnalystProposalsBundle = field(default_factory=AnalystProposalsBundle)
    regimes: RegimeBundle = field(default_factory=RegimeBundle)


# ---------------------------------------------------------------------------
# Extension loader hooks (pre-declared seams; stubbed empty this story)
# ---------------------------------------------------------------------------


async def _load_refs(session: AsyncSession, agent_calls: tuple[AgentCallRecord, ...]) -> RefsBundle:
    """Citation-reference sub-bundle loader hook (story 06d / ALP-886).

    The imperative shell for the citation chain: over the window's already-loaded
    *agent_calls* (read once by :func:`load_window`), reads each call's
    ``output.json`` provenance artifact, extracts the cited upstream refs per layer
    (synthesizer brief vs. decision-layer narratives), joins to the resolved thesis
    components via *session*, and delegates the pure assembly to
    :func:`~alphamind.feedback_loop.citation.parser.assemble_chains`. The file read
    is the sole impurity; parsing and chain assembly are pure.
    """
    synthesis_text = ""
    universe: set[str] = set()
    decision_citations: set[str] = set()
    for call in agent_calls:
        payload = _read_output_payload(call.output_artifact_ref)
        if payload is None:
            continue
        if call.agent_name == AgentName.synthesizer.value:
            synthesis_text = _synthesis_text(payload)
            universe |= _retrieval_store_refs(payload)
        elif call.agent_name in _DECISION_AGENT_NAMES:
            decision_citations |= set(extract_payload_citations(payload))
    components = await session.run_sync(
        lambda sync_session: _read_component_citations(sync_session)
    )
    citations = assemble_chains(
        universe=universe,
        synthesis_text=synthesis_text,
        decision_citations=decision_citations,
        components=components,
    )
    return RefsBundle(citations=citations)


#: The decision-layer agents whose narratives cite upstream refs (the chain's
#: "cited in decision narrative" measurement point).
_DECISION_AGENT_NAMES: frozenset[str] = frozenset(
    {
        AgentName.analyst.value,
        AgentName.strategist.value,
        AgentName.portfolio_manager.value,
    }
)


def _read_output_payload(output_artifact_ref: str | None) -> object | None:
    """Read the ``output.json`` structured payload under *output_artifact_ref*.

    ``output_artifact_ref`` is the per-call provenance directory; the structured
    output lives in ``output.json`` (see ``analysis/_agent_call_capture.py``).
    Returns ``None`` when the ref is absent, the file is missing, or its payload
    is ``null`` / unreadable — the citation read is best-effort and never fails
    the loader on a malformed artifact.
    """
    if output_artifact_ref is None:
        return None
    output_path = Path(output_artifact_ref) / "output.json"
    try:
        raw = output_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload


def _synthesis_text(payload: object) -> str:
    """Extract the synthesizer brief body from its output payload."""
    if isinstance(payload, dict):
        text = payload.get("synthesis_text")
        if isinstance(text, str):
            return text
    return ""


def _retrieval_store_refs(payload: object) -> set[str]:
    """Extract the universe of upstream refs from the synthesizer retrieval store.

    The retrieval store is keyed by full prefixed ref ID; its keys are the
    authoritative set of upstream refs available that window (the per-source
    rate denominators).
    """
    if not isinstance(payload, dict):
        return set()
    store = payload.get("retrieval_store")
    if not isinstance(store, dict):
        return set()
    entries = store.get("entries")
    if not isinstance(entries, dict):
        return set()
    return {key for key in entries if isinstance(key, str)}


def _read_component_citations(session: Session) -> tuple[ComponentCitation, ...]:
    """Read resolved thesis components and the upstream refs each narrative cites.

    The chain terminus. ``feedback_loop`` is read-only over trading state, so
    this is a plain windowless read of the component rows (a component's
    resolution is point-in-time, not window-bounded); the per-ref join filters
    to refs the component actually cites.
    """
    rows = session.execute(select(ThesisComponentRow)).scalars().all()
    citations: list[ComponentCitation] = []
    for row in rows:
        cited = set(extract_payload_citations(row.narrative))
        cited |= set(extract_payload_citations(row.supporting_signals_json))
        if not cited:
            continue
        outcome = (
            ThesisComponentOutcome(row.resolution_outcome)
            if row.resolution_outcome is not None
            else None
        )
        citations.append(ComponentCitation(cited_refs=frozenset(cited), resolution_outcome=outcome))
    return tuple(citations)


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
    """Resolved-thesis outcome sub-bundle loader hook (story 06c / ALP-919).

    Reads the RESOLVED theses resolved in ``[start, end)`` and projects each
    into a :class:`ThesisOutcome`. Conditioning attributes for ``regime`` and
    ``time_of_day`` are populated from the thesis's generating invocation
    (ALP-919 / story 02i): the non-NULL ``invocation_id``s are collected,
    the ``invocations`` table is queried once for their ``active_regime`` and
    ``trigger_source``, and the map is threaded into each :class:`ThesisOutcome`.
    Theses whose ``invocation_id`` is NULL (pre-migration rows) receive
    ``regime=None`` / ``time_of_day=None``. The other six conditioning dimensions
    remain empty until ALP-906 lands them. When *config* is supplied its
    sample-size thresholds are stamped onto the bundle; otherwise the packaged
    defaults stand.
    """
    resolved = await read_resolved_theses_in_window(session, start, end)
    # Collect non-NULL generating invocation IDs for the batch conditioning read.
    inv_ids = [str(r.invocation_id) for r in resolved if r.invocation_id is not None]
    conditioning_map = await read_invocation_conditioning(session, inv_ids)
    theses = tuple(_thesis_to_outcome(record, conditioning_map) for record in resolved)
    if config is None:
        return OutcomesBundle(theses=theses)
    return OutcomesBundle(
        theses=theses,
        min_resolved_theses_monthly=config.min_resolved_theses_monthly,
        min_resolved_theses_quarterly=config.min_resolved_theses_quarterly,
    )


def _thesis_to_outcome(
    record: ThesisRecord,
    conditioning_map: Mapping[str, InvocationConditioning] | None = None,
) -> ThesisOutcome:
    """Project a resolved ``ThesisRecord`` into a flat :class:`ThesisOutcome`.

    The realized resolution facts are guaranteed non-``None`` for a RESOLVED record
    by ``ThesisRecord``'s own validators. ``regime`` and ``time_of_day`` are
    populated from the thesis's generating invocation via *conditioning_map*
    (ALP-919 / story 02i); a thesis with no ``invocation_id`` or with an id absent
    from the map yields ``None`` for both. The other six conditioning dimensions
    remain empty until ALP-906 lands them.
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
    inv_cond: InvocationConditioning | None = None
    if conditioning_map is not None and record.invocation_id is not None:
        inv_cond = conditioning_map.get(str(record.invocation_id))
    return ThesisOutcome(
        thesis_id=record.thesis_id,
        position_id=record.position_id,
        resolution_category=record.resolution_category,
        resolution_pnl_usd=record.resolution_pnl_usd,
        active_duration_hours=active_duration_hours,
        expected_duration_hours=record.time_expectation_hours,
        conditioning=ConditioningAttributes(
            regime=inv_cond.regime if inv_cond is not None else None,
            time_of_day=inv_cond.time_of_day if inv_cond is not None else None,
        ),
    )


# ---------------------------------------------------------------------------
# Cost / execution-process loader hooks (story 06b — ALP-884)
#
# A distinct region from the citation (_load_refs) / outcomes (_load_outcomes) hooks so
# the concurrent 06g edits to _load_refs/load_window never collide here.
# ---------------------------------------------------------------------------

#: The activity-log event types the execution-process metrics count over the window. The
#: PM_DECISION envelopes are included for the engine-originated-CLOSE cascade join (a
#: cascade close's POSITION_CLOSED carries the ``engine_guardrail`` envelope's command id
#: as its ``order_id``); the metric cores live in
#: :mod:`alphamind.feedback_loop.metrics.cost_execution`.
_EXECUTION_PROCESS_EVENT_TYPES: tuple[EventType, ...] = (
    EventType.GUARDRAIL_REJECTION,
    EventType.COMMAND_ABANDONED,
    EventType.POSITION_CLOSED,
    EventType.PM_DECISION,
)


async def _load_activity_events(
    session: AsyncSession, start: datetime, end: datetime
) -> ActivityEventsBundle:
    """Window-bounded activity-event sub-bundle loader hook (story 06b).

    Reads the ``[start, end)`` slice of the activity-log event types the
    execution-process metrics count — distinct from the ``PM_DECISION``-only sliding
    window (:func:`read_recent_pm_decision_log`), which is count-based and not strictly
    window-bounded.
    """
    entries = await read_activity_events_in_window(
        session, start, end, _EXECUTION_PROCESS_EVENT_TYPES
    )
    return ActivityEventsBundle(entries=entries)


# ---------------------------------------------------------------------------
# Decision-layer loader hooks deferred from 06a (story 06f — ALP-911)
#
# A distinct region from the citation (_load_refs) / 06b / outcomes hooks so the
# concurrent dataset.py edits never collide here.
# ---------------------------------------------------------------------------


async def _load_analyst_proposals(
    session: AsyncSession, start: datetime, end: datetime
) -> AnalystProposalsBundle:
    """Per-invocation analyst proposal-count sub-bundle loader hook (story 06f).

    Over each **successful** analyst ``agent_calls`` run in ``[start, end)``, reads
    its ``output.json`` (``AnalystOutput``) and records one observation per
    *invocation* carrying ``proposal_count`` = ``len(recommendations)`` for a
    normal-mode run, or ``0`` for a watchlist-mode run (a deliberate no-new-entry
    invocation — an inaction). A call whose payload is absent / unparseable is
    skipped (a failed analyst call aborts the whole invocation per the
    uniform-criticality invariant, so it is not a measurable invocation); retries
    are deduped by ``invocation_id`` (one observation per invocation, first wins).
    """
    calls = await read_agent_calls_for_agent(session, AgentName.analyst.value, start, end)
    by_invocation: dict[str, AnalystProposalObservation] = {}
    for call in calls:
        if not call.success or call.invocation_id in by_invocation:
            continue
        payload = _read_output_payload(call.output_artifact_ref)
        count = _analyst_proposal_count(payload)
        if count is None:
            continue
        by_invocation[call.invocation_id] = AnalystProposalObservation(
            invocation_id=call.invocation_id, proposal_count=count
        )
    return AnalystProposalsBundle(observations=tuple(by_invocation.values()))


def _analyst_proposal_count(payload: object) -> int | None:
    """Proposal count for one analyst ``output.json`` payload, or ``None`` to skip.

    A normal-mode run contributes ``len(recommendations)``; a watchlist-mode run
    contributes ``0`` (a deliberate inaction). An absent / unparseable payload, or
    one whose ``mode`` is neither, is unmeasurable and skipped.
    """
    if not isinstance(payload, dict):
        return None
    mode = payload.get("mode")
    if mode == "watchlist":
        return 0
    if mode == "normal":
        recommendations = payload.get("recommendations")
        return len(recommendations) if isinstance(recommendations, list) else 0
    return None


async def _load_regimes(session: AsyncSession, start: datetime, end: datetime) -> RegimeBundle:
    """Per-invocation active-regime sub-bundle loader hook (story 06f).

    Reads the ``invocation_id -> active_regime`` map over invocations started in
    ``[start, end)`` — the REGIME conditioning source the decision metrics resolve
    a held regime through.
    """
    by_invocation = await read_invocation_regimes_in_window(session, start, end)
    return RegimeBundle(by_invocation=by_invocation)


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
    * validations superseded within ``[start, end)`` (the digest's
      ``validation_superseded`` shift surface).
    * the resolved-thesis ``outcomes`` sub-bundle, stamped with *config*'s
      sample-size thresholds (the packaged defaults when *config* is omitted).
    * the ``activity_events`` sub-bundle — the strictly-window-bounded slice of the
      execution-process event types (story 06b).
    * the ``refs`` / ``replays`` extension sub-bundles via their hooks (empty
      until 06d / 06e fill them).

    The ``budgets`` sub-bundle is the packaged ``agents.yaml`` latency envelope — read
    once via its default factory; it carries no window-bounded state.

    ``validation_queries`` exposes synchronous ``Session``-based helpers; they are
    bridged onto this async session via :meth:`AsyncSession.run_sync` — SQLAlchemy
    2.x's canonical sync bridge over the same connection.
    """
    agent_calls = await read_agent_calls_in_window(session, start, end)
    pm_decision_log = await read_recent_pm_decision_log(session, _PM_DECISION_SLIDING_WINDOW)
    validations = await session.run_sync(
        lambda sync_session: read_pending_validations(sync_session)
    )
    superseded_validations = await session.run_sync(
        lambda sync_session: read_validations_superseded_in_window(sync_session, start, end)
    )
    refs = await _load_refs(session, agent_calls)
    replays = await _load_replays(session, start, end)
    outcomes = await _load_outcomes(session, start, end, config)
    activity_events = await _load_activity_events(session, start, end)
    analyst_proposals = await _load_analyst_proposals(session, start, end)
    regimes = await _load_regimes(session, start, end)
    return WindowDataset(
        start=start,
        end=end,
        agent_calls=agent_calls,
        pm_decision_log=pm_decision_log,
        validations=validations,
        superseded_validations=superseded_validations,
        refs=refs,
        replays=replays,
        outcomes=outcomes,
        activity_events=activity_events,
        analyst_proposals=analyst_proposals,
        regimes=regimes,
    )


__all__ = [
    "ActivityEventsBundle",
    "AnalystProposalObservation",
    "AnalystProposalsBundle",
    "ConditioningAttributes",
    "CostBudgetsBundle",
    "OutcomesBundle",
    "RefsBundle",
    "RegimeBundle",
    "ReplaysBundle",
    "ThesisOutcome",
    "WindowDataset",
    "load_window",
]
