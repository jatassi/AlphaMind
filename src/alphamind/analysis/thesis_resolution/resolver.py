"""Closed-position thesis resolver — ALP-899 (story 04e), hardened ALP-914 (04h).

Closes the ALP-834 loop: execution leaves theses ``ACTIVE`` on position
close; this resolver — wired as a step in ``scheduler/orchestrator.py``'s
``run_invocation`` after fill collection and before snapshot assembly —
authors the ``ACTIVE → RESOLVED`` transition the portfolio-state read model
requires.

Two phases (ALP-914 finding 2):

* **Read/assess** (:func:`prepare_closed_position_resolutions`, no write lock):
  on a read session, bulk-fetch the eligible theses + components, their
  ``POSITION_CLOSED`` exit methods, their ``thesis_pnl_ledger`` rows, and each
  closed position's entry reference (all keyed by the eligible id set — no
  per-thesis N+1). Run the programmatic (story 04c) + LLM-fallback (story 04d)
  component assessment over a focused market-data slice, applying the
  per-thesis gap skips (finding 1). Build a tuple of :class:`PreparedResolution`.
* **Persist** (:func:`persist_thesis_resolutions`, inside the caller's write
  transaction): batch-``merge`` the resolved thesis + component rows and append
  one ``THESIS_RESOLVED`` entry per resolved thesis. No LLM I/O, no
  exit-method / ledger reads occur here.

For each ``ACTIVE`` thesis whose linked position is ``CLOSED`` the read phase:

* assesses every component — programmatically for falsifiable quantitative
  components, falling back to the targeted LLM evaluator for the ones the
  assessor leaves ``INCONCLUSIVE`` (all ``ENTRY_RATIONALE`` components and any
  quantitative component an external exit made indeterminate),
* reads realized P/L from ``thesis_pnl_ledger`` and the exit method from the
  position's ``POSITION_CLOSED`` activity-log entry,
* classifies the thesis via the relocated ``classify_thesis_resolution``,

and the write phase writes the resolved ``ThesisRecord`` + component outcomes
via ``session.merge`` (PKs match, so the existing rows update in place) and
emits one real ``THESIS_RESOLVED`` activity-log entry.

A per-thesis data gap (missing ``POSITION_CLOSED`` entry, a ``POSITION_CLOSED``
entry whose detail cannot be read as a ``PositionClosedDetail``, or a missing
``thesis_pnl_ledger`` row) logs a ``WARNING`` and **skips** that thesis (left
``ACTIVE``) — it never raises out of the per-thesis path and never aborts the
invocation (ALP-914 finding 1).

This is the one analysis-layer module sanctioned to mutate trading state (see
``.importlinter`` ``feedback_loop-no-execution`` rationale).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from alphamind.portfolio_state.records.positions import (
    PositionStatus,
    resolve_ticker,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.portfolio_state.records.thesis_resolution import (
    classify_thesis_resolution,
)
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_from_row,
    append_activity_log_entry,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import record_to_rows, rows_to_record
from alphamind.state.tables.thesis_components import ThesisComponentRow
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow

log = logging.getLogger(__name__)

__all__ = [
    "PreparedResolution",
    "ResolvedThesis",
    "persist_thesis_resolutions",
    "prepare_closed_position_resolutions",
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


@dataclass(frozen=True, slots=True)
class PreparedResolution:
    """A read/assess-phase result ready to persist — no further I/O needed.

    Carries the fully-resolved ``ThesisRecord`` (status RESOLVED, every
    component outcome stamped, category + P/L set) and the
    ``ThesisResolvedDetail`` to emit. :func:`persist_thesis_resolutions`
    ``merge``s the record and appends the entry inside the write transaction.
    """

    record: ThesisRecord
    detail: ThesisResolvedDetail


@dataclass(frozen=True, slots=True)
class _EntryReference:
    """The closed position's entry reference for the market-data slice.

    ``symbol`` is the key into ``underlying_prices``; ``entry_price`` is the
    average cost basis per share at entry. ``None`` when the position's details
    carry no equity entry reference (e.g. a multi-leg strategy) — the slice then
    degrades to the assumptions-only framing.
    """

    symbol: str | None
    entry_price: float | None


class _LazyEvaluatorConfig:
    """Memoizing factory wrapper — builds the ``BaseAgentConfig`` at most once.

    The config's construction file-stats the prompt via ``prompt_path_exists``,
    so it must not be built on a zero-thesis or all-programmatic invocation
    (ALP-914 finding 4). The wrapped factory is invoked the first time a
    component actually needs the LLM fallback, and the result is cached.
    """

    def __init__(self, factory: Callable[[], BaseAgentConfig]) -> None:
        self._factory = factory
        self._config: BaseAgentConfig | None = None

    def get(self) -> BaseAgentConfig:
        if self._config is None:
            self._config = self._factory()
        return self._config


@dataclass(frozen=True, slots=True)
class _LLMEvalContext:
    """The harness kwargs the LLM-fallback evaluator (04d) needs, bundled once.

    Threaded unchanged through the per-thesis loop so the assessment helper
    stays a small function rather than re-listing the harness contract.
    """

    evaluator_config: _LazyEvaluatorConfig
    invocation_id: str
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None
    archive_root: Path | None
    now: datetime
    progress: ProgressEmitter
    telemetry_session_factory: async_sessionmaker[AsyncSession] | None
    provenance_root: Path | None


async def prepare_closed_position_resolutions(  # noqa: PLR0913 — read session + the harness kwargs (config / now / prices / sdk_fn / archive / progress) the resolver threads to every evaluator call, now incl. the ALP-922 telemetry factory + provenance_root
    read_session: AsyncSession,
    *,
    invocation_id: str,
    evaluator_config_factory: Callable[[], BaseAgentConfig],
    now: datetime,
    underlying_prices: Mapping[str, float] | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    archive_root: Path | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    telemetry_session_factory: async_sessionmaker[AsyncSession] | None = None,
    provenance_root: Path | None = None,
) -> tuple[PreparedResolution, ...]:
    """Read + assess every closed-position ``ACTIVE`` thesis (no write lock).

    Bulk-fetches the eligible theses, their exit methods, ledger rows, and entry
    references in one pass each (no per-thesis N+1), assesses each component
    (programmatic + LLM fallback over a focused market-data slice), and returns
    the prepared resolutions ready for :func:`persist_thesis_resolutions`. A
    per-thesis data gap logs a ``WARNING`` and skips that thesis (ALP-914
    finding 1) — it is never raised, so a gap never aborts the invocation.

    No write to the session occurs here; the caller persists in a separate
    short IMMEDIATE write transaction.

    When both ``telemetry_session_factory`` and ``provenance_root`` are wired
    (the telemetry-active production path), each LLM-fallback component
    evaluation opens a fresh best-effort session from the factory so its
    ``agent_calls`` row + provenance artifacts are captured (ALP-922), exactly
    as the roster agents under ALP-907. That telemetry session is independent
    of ``read_session``, preserving the read phase's no-write invariant. When
    either is absent, ``capture_agent_call`` stays a clean no-op.
    """
    prices: Mapping[str, float] = underlying_prices if underlying_prices is not None else {}
    llm_context = _LLMEvalContext(
        evaluator_config=_LazyEvaluatorConfig(evaluator_config_factory),
        invocation_id=invocation_id,
        sdk_query_fn=sdk_query_fn,
        archive_root=archive_root,
        now=now,
        progress=progress,
        telemetry_session_factory=telemetry_session_factory,
        provenance_root=provenance_root,
    )

    active_thesis_rows = await _read_active_theses_with_closed_positions(read_session)
    if not active_thesis_rows:
        return ()

    position_ids = [t.position_id for t, _ in active_thesis_rows]
    thesis_ids = [t.thesis_id for t, _ in active_thesis_rows]
    exit_methods = await _read_exit_methods(read_session, position_ids=position_ids)
    realized_pnls = await _read_realized_pnls(read_session, thesis_ids=thesis_ids)
    entry_refs = await _read_entry_references(read_session, position_ids=position_ids)

    prepared: list[PreparedResolution] = []
    for thesis_row, component_rows in active_thesis_rows:
        record = rows_to_record(thesis_row, tuple(component_rows))
        position_id = str(record.position_id)
        thesis_id = str(record.thesis_id)

        exit_method = exit_methods.get(position_id)
        if exit_method is None:
            log.warning(
                "thesis-resolution skip: thesis_id=%s position_id=%s — no readable "
                "POSITION_CLOSED exit method; leaving ACTIVE",
                thesis_id,
                position_id,
            )
            continue
        realized_pnl_usd = realized_pnls.get(thesis_id)
        if realized_pnl_usd is None:
            log.warning(
                "thesis-resolution skip: thesis_id=%s position_id=%s — no "
                "thesis_pnl_ledger row; leaving ACTIVE",
                thesis_id,
                position_id,
            )
            continue

        component_outcomes = await _assess_components(
            record,
            exit_method=exit_method,
            realized_pnl_usd=realized_pnl_usd,
            entry_reference=entry_refs.get(position_id, _EntryReference(None, None)),
            underlying_prices=prices,
            llm_context=llm_context,
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
        prepared.append(
            PreparedResolution(
                record=resolved_record,
                detail=_build_thesis_resolved_detail(resolved_record),
            )
        )
    return tuple(prepared)


async def persist_thesis_resolutions(
    handle: InvocationHandle,
    prepared: tuple[PreparedResolution, ...],
    *,
    now: datetime,
) -> tuple[ResolvedThesis, ...]:
    """Persist the prepared resolutions inside the caller's write transaction.

    Batch-``merge``s each resolved thesis + component row and appends one
    ``THESIS_RESOLVED`` entry per resolution. Contains no LLM I/O and no
    exit-method / ledger reads — the slow + read work happened in
    :func:`prepare_closed_position_resolutions`. The caller commits.
    """
    session = handle.session
    rows_to_merge: list[Any] = []
    for item in prepared:
        thesis_row, component_rows = record_to_rows(item.record)
        rows_to_merge.append(thesis_row)
        rows_to_merge.extend(component_rows)
    for row in rows_to_merge:
        await session.merge(row)
    resolved: list[ResolvedThesis] = []
    for item in prepared:
        _emit_thesis_resolved(handle, item.record, detail=item.detail, now=now)
        resolved.append(ResolvedThesis(record=item.record, detail=item.detail))
    return tuple(resolved)


async def resolve_closed_position_theses(
    handle: InvocationHandle,
    *,
    evaluator_config: BaseAgentConfig | None = None,
    evaluator_config_factory: Callable[[], BaseAgentConfig] | None = None,
    now: datetime | None = None,
    underlying_prices: Mapping[str, float] | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    archive_root: Path | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
) -> tuple[ResolvedThesis, ...]:
    """Resolve every closed-position ``ACTIVE`` thesis on the handle's session.

    Single-session convenience wrapper that runs the read/assess phase and the
    persist phase on the same session (used by tests and any caller that does
    not need the read/write transaction split). Returns the resolved theses
    (empty when none are eligible — a clean no-op); the caller commits.

    Pass exactly one of ``evaluator_config`` (eager) or
    ``evaluator_config_factory`` (lazy — built only when a component first needs
    the LLM fallback, ALP-914 finding 4). ``now`` defaults to
    ``datetime.now(UTC)``. ``underlying_prices`` feeds the market-data slice
    (ALP-914 finding 5). ``sdk_query_fn`` is threaded to the LLM evaluator;
    tests inject a stub so no test touches the Anthropic API.
    """
    if now is None:
        from datetime import UTC

        now = datetime.now(UTC)
    factory = _resolve_config_factory(
        evaluator_config=evaluator_config,
        evaluator_config_factory=evaluator_config_factory,
    )
    prepared = await prepare_closed_position_resolutions(
        handle.session,
        invocation_id=handle.invocation_id,
        evaluator_config_factory=factory,
        now=now,
        underlying_prices=underlying_prices,
        sdk_query_fn=sdk_query_fn,
        archive_root=archive_root,
        progress=progress,
    )
    return await persist_thesis_resolutions(handle, prepared, now=now)


def _resolve_config_factory(
    *,
    evaluator_config: BaseAgentConfig | None,
    evaluator_config_factory: Callable[[], BaseAgentConfig] | None,
) -> Callable[[], BaseAgentConfig]:
    """Normalize the two evaluator-config inputs into a single factory.

    Exactly one of the two must be supplied; an eager ``evaluator_config`` is
    wrapped in a thunk so the lazy-build path stays uniform.
    """
    if (evaluator_config is None) == (evaluator_config_factory is None):
        msg = "pass exactly one of evaluator_config or evaluator_config_factory"
        raise ValueError(msg)
    if evaluator_config_factory is not None:
        return evaluator_config_factory
    config = evaluator_config
    assert config is not None
    return lambda: config


# ---------------------------------------------------------------------------
# Reads (bulk — one query each, keyed by the eligible id set; no per-thesis N+1)
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


async def _read_exit_methods(
    session: AsyncSession, *, position_ids: list[str]
) -> dict[str, PositionExitMethod]:
    """Map each position_id to its most-recent POSITION_CLOSED exit method.

    A position with no POSITION_CLOSED entry, or one whose detail cannot be
    decoded as a ``PositionClosedDetail``, is simply absent from the result —
    the per-thesis path then skips it with a WARNING (ALP-914 finding 1). One
    query over the whole eligible set replaces the per-thesis N+1 read.
    """
    if not position_ids:
        return {}
    stmt = (
        select(ActivityLogRow)
        .where(
            ActivityLogRow.position_id.in_(position_ids),
            ActivityLogRow.event_type == EventType.POSITION_CLOSED.value,
        )
        .order_by(ActivityLogRow.entry_at.desc(), ActivityLogRow.entry_id.desc())
    )
    exit_methods: dict[str, PositionExitMethod] = {}
    seen: set[str] = set()
    for row in (await session.execute(stmt)).scalars():
        position_id = row.position_id
        if position_id is None or position_id in seen:
            continue  # rows are newest-first; only the newest entry per position is authoritative
        seen.add(position_id)
        # The newest POSITION_CLOSED entry decides the exit method; if it fails to
        # decode the position is left absent (skipped with a WARNING by the caller)
        # rather than silently falling through to a stale older entry.
        method = _decode_exit_method(row)
        if method is not None:
            exit_methods[position_id] = method
    return exit_methods


def _decode_exit_method(row: ActivityLogRow) -> PositionExitMethod | None:
    """Decode the exit method from a POSITION_CLOSED row, or ``None`` on a gap.

    Guards the bare ``assert isinstance(...)`` the original resolver used
    (ALP-914 finding 1): a row whose detail cannot be read as a
    ``PositionClosedDetail`` — because the payload fails to decode or decodes to
    an unexpected type — returns ``None`` (a skip) rather than raising. Survives
    ``python -O`` (no ``assert`` involved).
    """
    try:
        detail = activity_log_entry_from_row(row).detail
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(detail, PositionClosedDetail):
        return None
    return detail.exit_method


async def _read_realized_pnls(session: AsyncSession, *, thesis_ids: list[str]) -> dict[str, float]:
    """Map each thesis_id to its realized P/L from ``thesis_pnl_ledger``.

    A thesis with no ledger row is absent from the result (skipped with a
    WARNING by the caller). One ``IN`` query replaces the per-thesis ``get``.
    """
    if not thesis_ids:
        return {}
    stmt = select(ThesisPnlLedgerRow).where(ThesisPnlLedgerRow.thesis_id.in_(thesis_ids))
    return {
        row.thesis_id: float(row.realized_pnl_usd)
        for row in (await session.execute(stmt)).scalars()
    }


async def _read_entry_references(
    session: AsyncSession, *, position_ids: list[str]
) -> dict[str, _EntryReference]:
    """Map each closed position_id to its entry reference (symbol + entry price).

    Reads the position's average cost basis per share + underlying symbol for
    the market-data slice (ALP-914 finding 5). One ``IN`` query; a position
    whose details carry no equity entry reference maps to a null reference.
    """
    if not position_ids:
        return {}
    stmt = select(PositionRow).where(PositionRow.position_id.in_(position_ids))
    refs: dict[str, _EntryReference] = {}
    for row in (await session.execute(stmt)).scalars():
        try:
            record = row_to_record(row)
        except (TypeError, ValueError, KeyError):
            # Entry refs feed only the best-effort market-data slice. A position
            # row that fails reconstruction (e.g. a violated PositionRecord
            # invariant on a malformed/legacy row) must not raise out of the
            # read phase and wedge the whole resolution pass (ALP-914 finding 1)
            # — skip it; the slice falls back to assumptions-only for that thesis.
            continue
        symbol = resolve_ticker(record.details)
        entry_price = getattr(record.details, "average_cost_basis_per_share", None)
        refs[row.position_id] = _EntryReference(
            symbol=str(symbol) if symbol is not None else None,
            entry_price=float(entry_price) if entry_price is not None else None,
        )
    return refs


# ---------------------------------------------------------------------------
# Component assessment — programmatic 04c, LLM fallback 04d
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _telemetry_session(
    factory: async_sessionmaker[AsyncSession] | None,
    provenance_root: Path | None,
) -> AsyncIterator[AsyncSession | None]:
    """Open one per-call best-effort telemetry session for agent_calls capture (ALP-922).

    Yields a fresh :class:`AsyncSession` from the orchestrator's in-process
    *factory* when both *factory* and *provenance_root* are wired, else ``None``
    so the harness's ``capture_agent_call`` stays a no-op. The ``agent_calls``
    row ``persist_agent_call`` queues during the harness's capture drain is
    committed here, then the session is closed — on both the success and the
    failure path.

    A telemetry commit/close error is logged and swallowed: capture is a
    best-effort append and must never break the evaluation it observes,
    extending ``_drain_capture``'s "capture never breaks the call it observes"
    contract to the commit. This mirrors the subprocess worker's per-call
    ``_telemetry_session`` (ALP-907) but sources the session from the
    orchestrator's canonical ``session_factory`` rather than a dedicated engine
    — the resolver runs in-process. A plain per-call session, NOT
    ``begin_write_immediate`` / ``run_with_sqlite_busy_retry``: the row is a
    best-effort append, exactly as ALP-907.
    """
    if factory is None or provenance_root is None:
        yield None
        return
    session = factory()
    try:
        yield session
    finally:
        try:
            await session.commit()
        except Exception:
            log.exception("agent_calls telemetry commit failed in thesis resolver")
        finally:
            with contextlib.suppress(Exception):
                await session.close()


async def _assess_components(
    record: ThesisRecord,
    *,
    exit_method: PositionExitMethod,
    realized_pnl_usd: float,
    entry_reference: _EntryReference,
    underlying_prices: Mapping[str, float],
    llm_context: _LLMEvalContext,
) -> dict[str, ThesisComponentOutcome]:
    """Map each component_id to a resolved outcome.

    Programmatic first (zero tokens). A component the assessor leaves
    ``INCONCLUSIVE`` is qualitative/ambiguous → the targeted LLM evaluator
    (04d) refines it over a focused market-data slice the resolver renders. The
    evaluator config is built lazily, only when the first such component appears.

    Each LLM-fallback evaluation opens a fresh per-call telemetry session
    (ALP-922) so its ``agent_calls`` row + provenance artifacts are captured
    when telemetry is wired; capture is best-effort and never aborts the
    evaluation. When telemetry is unwired the session is ``None`` and
    ``capture_agent_call`` stays a no-op.
    """
    market_data = _render_market_data_slice(
        exit_method=exit_method,
        realized_pnl_usd=realized_pnl_usd,
        entry_reference=entry_reference,
        underlying_prices=underlying_prices,
    )
    outcomes: dict[str, ThesisComponentOutcome] = {}
    for component in record.components:
        programmatic = assess_component_programmatically(component, exit_method)
        if programmatic is not ThesisComponentOutcome.INCONCLUSIVE:
            outcomes[component.component_id] = programmatic
            continue
        async with _telemetry_session(
            llm_context.telemetry_session_factory, llm_context.provenance_root
        ) as telemetry_session:
            llm_outcome = await evaluate_component_llm(
                component,
                market_data,
                agent_config=llm_context.evaluator_config.get(),
                invocation_id=llm_context.invocation_id,
                as_of=llm_context.now,
                archive_root=llm_context.archive_root,
                sdk_query_fn=llm_context.sdk_query_fn,
                progress=llm_context.progress,
                telemetry_session=telemetry_session,
                provenance_root=llm_context.provenance_root,
            )
        outcomes[component.component_id] = llm_outcome.outcome
    return outcomes


def _render_market_data_slice(
    *,
    exit_method: PositionExitMethod,
    realized_pnl_usd: float,
    entry_reference: _EntryReference,
    underlying_prices: Mapping[str, float],
) -> str:
    """Render the focused market-data slice for the LLM evaluator (pure).

    Surfaces the price path — instrument, entry price, resolution-time price,
    and the move (absolute + percent) — so an ``ENTRY_RATIONALE`` evaluation
    reasons about whether the entry thesis *played out* rather than anchoring on
    P/L; the realized P/L is framed as the trade's outcome, not the lead
    (ALP-914 finding 5). When the instrument's resolution-time price is
    unavailable (symbol absent from ``underlying_prices``, or no entry
    reference), the slice falls back to an assumptions-only framing and never
    raises.
    """
    symbol = entry_reference.symbol
    entry_price = entry_reference.entry_price
    resolution_price = underlying_prices.get(symbol) if symbol is not None else None

    lines: list[str] = []
    if symbol is not None and entry_price is not None and resolution_price is not None:
        move_abs = resolution_price - entry_price
        move_pct = (move_abs / entry_price * 100.0) if entry_price else 0.0
        lines.append(f"Instrument: {symbol}")
        lines.append(f"Entry price: ${entry_price:,.2f}")
        lines.append(f"Resolution-time price: ${resolution_price:,.2f}")
        lines.append(f"Price move: ${move_abs:,.2f} ({move_pct:+.1f}%)")
    else:
        instrument = symbol if symbol is not None else "(instrument unknown)"
        lines.append(f"Instrument: {instrument}")
        if entry_price is not None:
            lines.append(f"Entry price: ${entry_price:,.2f}")
        lines.append("Resolution-time price unavailable.")
    lines.append(f"Position closed via {exit_method.value}.")
    lines.append(f"Trade outcome (not the lead): realized P/L ${realized_pnl_usd:,.2f}.")
    return "\n".join(lines)


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


def _build_thesis_resolved_detail(record: ThesisRecord) -> ThesisResolvedDetail:
    """Build the ``THESIS_RESOLVED`` detail from a resolved record (pure)."""
    assert record.resolution_category is not None
    return ThesisResolvedDetail(
        resolution_category=record.resolution_category.value,
        component_outcomes_json={
            c.component_id: c.resolution_outcome.value
            for c in record.components
            if c.resolution_outcome is not None
        },
    )


def _emit_thesis_resolved(
    handle: InvocationHandle,
    record: ThesisRecord,
    *,
    detail: ThesisResolvedDetail,
    now: datetime,
) -> None:
    """Append one real THESIS_RESOLVED entry for the resolved thesis."""
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
