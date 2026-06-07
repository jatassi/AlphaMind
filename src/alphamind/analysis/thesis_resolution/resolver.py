"""Closed-position thesis resolver — ALP-899 (story 04e).

Closes the ALP-834 loop: execution leaves theses ``ACTIVE`` on position
close; this resolver — wired as a step in ``scheduler/orchestrator.py``'s
``run_invocation`` after fill collection and before snapshot assembly, in
its own transaction — authors the ``ACTIVE → RESOLVED`` transition the
portfolio-state read model requires.

For each ``ACTIVE`` thesis whose linked position is ``CLOSED`` it:

* assesses every component — programmatically (story 04c) for falsifiable
  quantitative components, falling back to the targeted LLM evaluator
  (story 04d) for the ones the assessor leaves ``INCONCLUSIVE`` (all
  ``ENTRY_RATIONALE`` components and any quantitative component an external
  exit made indeterminate),
* reads realized P/L from ``thesis_pnl_ledger`` and the exit method from the
  position's ``POSITION_CLOSED`` activity-log entry,
* classifies the thesis via the relocated ``classify_thesis_resolution``
  (story 04c, in ``portfolio_state`` so the import stays downward),
* writes the resolved ``ThesisRecord`` + component outcomes — satisfying
  ``ThesisRecord._check_resolved_fields`` — via ``session.merge`` (PKs match,
  so the existing rows update in place),
* emits one real ``THESIS_RESOLVED`` activity-log entry (a
  ``ThesisResolvedDetail`` carrying the category + per-component outcomes).

Reads and writes ride the caller-supplied :class:`InvocationHandle`'s session;
the caller owns the transaction boundary (commit/rollback). This is the one
analysis-layer module sanctioned to mutate trading state (see ``.importlinter``
``feedback_loop-no-execution`` rationale).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis.thesis_resolution.llm_evaluator import evaluate_component_llm
from alphamind.analysis.thesis_resolution.programmatic import (
    assess_component_programmatically,
)
from alphamind.config.models.agents import BaseAgentConfig
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.events.thesis import ThesisResolvedDetail
from alphamind.portfolio_state.records.positions import PositionStatus
from alphamind.portfolio_state.records.thesis_resolution import (
    classify_thesis_resolution,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_from_row,
    append_activity_log_entry,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import record_to_rows, rows_to_record
from alphamind.state.tables.thesis_components import ThesisComponentRow
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow

__all__ = [
    "ResolvedThesis",
    "resolve_closed_position_theses",
]


@dataclass(frozen=True, slots=True)
class ResolvedThesis:
    """One thesis the resolver moved ``ACTIVE → RESOLVED`` this invocation.

    Carries the persisted resolved record plus the ``THESIS_RESOLVED`` detail
    that was emitted — the observability handle the orchestrator returns and
    tests assert against.
    """

    record: ThesisRecord
    detail: ThesisResolvedDetail


async def resolve_closed_position_theses(
    handle: InvocationHandle,
    *,
    evaluator_config: BaseAgentConfig,
    now: datetime | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    archive_root: Path | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
) -> tuple[ResolvedThesis, ...]:
    """Resolve every closed-position ``ACTIVE`` thesis on the handle's session.

    Returns the resolved theses (empty when none are eligible — a clean no-op).
    Persists ``ACTIVE → RESOLVED`` + component outcomes and appends one
    ``THESIS_RESOLVED`` entry per resolved thesis to the open transaction; the
    caller commits.

    ``now`` defaults to ``datetime.now(UTC)`` — the resolution timestamp.
    ``sdk_query_fn`` is threaded to the LLM evaluator (defaults to the real
    SDK ``query``); tests inject a stub so no test touches the Anthropic API.
    """
    if now is None:
        from datetime import UTC

        now = datetime.now(UTC)

    session = handle.session
    active_thesis_rows = await _read_active_theses_with_closed_positions(session)

    resolved: list[ResolvedThesis] = []
    for thesis_row, component_rows in active_thesis_rows:
        record = rows_to_record(thesis_row, tuple(component_rows))
        exit_method = await _read_exit_method(session, position_id=str(record.position_id))
        realized_pnl_usd = await _read_realized_pnl(session, thesis_id=str(record.thesis_id))

        component_outcomes = await _assess_components(
            record,
            exit_method=exit_method,
            realized_pnl_usd=realized_pnl_usd,
            evaluator_config=evaluator_config,
            invocation_id=handle.invocation_id,
            sdk_query_fn=sdk_query_fn,
            archive_root=archive_root,
            now=now,
            progress=progress,
        )

        category = classify_thesis_resolution(
            tuple(component_outcomes[c.component_id] for c in record.components),
            realized_pnl_usd,
            exit_method,
        )
        resolved_record = _build_resolved_record(
            record,
            category=category,
            realized_pnl_usd=realized_pnl_usd,
            component_outcomes=component_outcomes,
            now=now,
        )
        await _persist_resolved(session, resolved_record)
        detail = _emit_thesis_resolved(handle, resolved_record, now=now)
        resolved.append(ResolvedThesis(record=resolved_record, detail=detail))

    return tuple(resolved)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _read_active_theses_with_closed_positions(
    session: AsyncSession,
) -> list[tuple[ThesisRow, list[ThesisComponentRow]]]:
    """ACTIVE theses whose linked position is CLOSED, each with its components."""
    stmt = (
        select(ThesisRow)
        .join(PositionRow, ThesisRow.position_id == PositionRow.position_id)
        .where(
            ThesisRow.status == ThesisRecordStatus.ACTIVE.value,
            PositionRow.status == PositionStatus.CLOSED.value,
        )
        .order_by(ThesisRow.thesis_id.asc())
    )
    thesis_rows = list((await session.execute(stmt)).scalars())
    if not thesis_rows:
        return []
    thesis_ids = [t.thesis_id for t in thesis_rows]
    comp_stmt = select(ThesisComponentRow).where(ThesisComponentRow.thesis_id.in_(thesis_ids))
    components_by_thesis: dict[str, list[ThesisComponentRow]] = {tid: [] for tid in thesis_ids}
    for comp in (await session.execute(comp_stmt)).scalars():
        components_by_thesis[comp.thesis_id].append(comp)
    return [(t, components_by_thesis[t.thesis_id]) for t in thesis_rows]


async def _read_exit_method(session: AsyncSession, *, position_id: str) -> PositionExitMethod:
    """The exit method from the position's most recent POSITION_CLOSED entry.

    The closer (fill collection or the continuous monitor) stamps the exit
    method on the ``POSITION_CLOSED`` activity-log entry, so the resolver reads
    it regardless of which subsystem closed the position.
    """
    stmt = (
        select(ActivityLogRow)
        .where(
            ActivityLogRow.position_id == position_id,
            ActivityLogRow.event_type == EventType.POSITION_CLOSED.value,
        )
        .order_by(ActivityLogRow.entry_at.desc(), ActivityLogRow.entry_id.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        msg = f"no POSITION_CLOSED activity-log entry for closed position {position_id!r}"
        raise ValueError(msg)
    detail = activity_log_entry_from_row(row).detail
    assert isinstance(detail, PositionClosedDetail)
    return detail.exit_method


async def _read_realized_pnl(session: AsyncSession, *, thesis_id: str) -> float:
    """Realized P/L for the thesis from ``thesis_pnl_ledger``."""
    row = await session.get(ThesisPnlLedgerRow, thesis_id)
    if row is None:
        msg = f"no thesis_pnl_ledger row for thesis {thesis_id!r}"
        raise ValueError(msg)
    return float(row.realized_pnl_usd)


# ---------------------------------------------------------------------------
# Component assessment — programmatic 04c, LLM fallback 04d
# ---------------------------------------------------------------------------


async def _assess_components(
    record: ThesisRecord,
    *,
    exit_method: PositionExitMethod,
    realized_pnl_usd: float,
    evaluator_config: BaseAgentConfig,
    invocation_id: str,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None,
    archive_root: Path | None,
    now: datetime,
    progress: ProgressEmitter,
) -> dict[str, ThesisComponentOutcome]:
    """Map each component_id to a resolved outcome.

    Programmatic first (zero tokens). A component the assessor leaves
    ``INCONCLUSIVE`` is qualitative/ambiguous → the targeted LLM evaluator
    (04d) refines it over a focused market-data slice the resolver renders.
    """
    market_data = _render_market_data_slice(
        exit_method=exit_method, realized_pnl_usd=realized_pnl_usd
    )
    outcomes: dict[str, ThesisComponentOutcome] = {}
    for component in record.components:
        programmatic = assess_component_programmatically(component, exit_method)
        if programmatic is not ThesisComponentOutcome.INCONCLUSIVE:
            outcomes[component.component_id] = programmatic
            continue
        llm_outcome = await evaluate_component_llm(
            component,
            market_data,
            agent_config=evaluator_config,
            invocation_id=invocation_id,
            as_of=now,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
            progress=progress,
            telemetry_session=None,
        )
        outcomes[component.component_id] = llm_outcome.outcome
    return outcomes


def _render_market_data_slice(
    *, exit_method: PositionExitMethod, realized_pnl_usd: float
) -> str:
    """Render the focused market-data slice for the LLM evaluator (pure).

    The lowest-coupling seam (story 04d takes ``market_data`` as ``str``): at
    resolution time the relevant facts are how the position closed — the exit
    method and the realized P/L outcome — which together let the evaluator
    judge whether a qualitative component held.
    """
    return (
        f"Position closed via {exit_method.value} with realized P/L "
        f"${realized_pnl_usd:,.2f}."
    )


# ---------------------------------------------------------------------------
# Build + persist + emit
# ---------------------------------------------------------------------------


def _build_resolved_record(
    record: ThesisRecord,
    *,
    category: Any,
    realized_pnl_usd: float,
    component_outcomes: dict[str, ThesisComponentOutcome],
    now: datetime,
) -> ThesisRecord:
    """Build the RESOLVED ThesisRecord from the ACTIVE one + resolution facts.

    Stamps each component's ``resolution_outcome`` and the thesis-level
    ``status`` / ``resolution_*`` fields; ``ThesisRecord.__post_init__``
    enforces ``_check_resolved_fields`` so an incomplete record fails closed.
    """
    resolved_components = tuple(
        dataclasses.replace(c, resolution_outcome=component_outcomes[c.component_id])
        for c in record.components
    )
    return dataclasses.replace(
        record,
        components=resolved_components,
        status=ThesisRecordStatus.RESOLVED,
        resolution_timestamp=now,
        resolution_category=category,
        resolution_pnl_usd=realized_pnl_usd,
    )


async def _persist_resolved(session: AsyncSession, record: ThesisRecord) -> None:
    """Update the existing thesis + component rows in place via merge.

    ``record_to_rows`` builds rows keyed by the same PKs as the persisted
    ACTIVE rows, so ``session.merge`` updates them rather than inserting.
    """
    thesis_row, component_rows = record_to_rows(record)
    await session.merge(thesis_row)
    for crow in component_rows:
        await session.merge(crow)


def _emit_thesis_resolved(
    handle: InvocationHandle, record: ThesisRecord, *, now: datetime
) -> ThesisResolvedDetail:
    """Append one real THESIS_RESOLVED entry for the resolved thesis."""
    assert record.resolution_category is not None
    detail = ThesisResolvedDetail(
        resolution_category=record.resolution_category.value,
        component_outcomes_json={
            c.component_id: c.resolution_outcome.value
            for c in record.components
            if c.resolution_outcome is not None
        },
    )
    entry = ActivityLogEntry(
        entry_id=f"{handle.invocation_id}-{EventType.THESIS_RESOLVED.value}-{uuid.uuid4().hex}",
        invocation_id=handle.invocation_id,
        timestamp=now,
        event_type=EventType.THESIS_RESOLVED,
        event_group=EVENT_TYPE_TO_GROUP[EventType.THESIS_RESOLVED],
        position_id=str(record.position_id),
        order_id=None,
        thesis_id=str(record.thesis_id),
        source=EventSource.ANALYSIS_PIPELINE,
        detail=detail,
    )
    append_activity_log_entry(handle, entry)
    return detail
