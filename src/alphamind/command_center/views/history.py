"""Run history view — ``GET /api/views/history/runs`` (story 05c / ALP-673).

Paginated, filterable list of past pipeline invocations from the
``invocations`` table. Filter dimensions: date range, run type,
status (completed / failed / partial), command-count-greater-than,
has-errors. Default sort: ``start_at DESC``. Page size capped at 100.

``GET /api/views/history/runs/preset/failure-log`` is a convenience
preset with ``status ∈ {failed, partial}`` pre-applied.

**Status derivation** — ``invocations`` carries no ``status`` column;
the view derives it from phase-completion timestamps:

* ``completed`` — ``phase2_completed_at IS NOT NULL``
* ``partial``   — ``phase1_completed_at IS NOT NULL`` and
                  ``phase2_completed_at IS NULL``
* ``failed``    — both completion timestamps are ``NULL``

Story 05d will extend this module by adding per-invocation detail
endpoints. Keep ``run_history_*`` prefixed names in this file and add
``invocation_detail_*`` prefixed names in any 05d additions.

All reads go through ``foreign_reader_session_factory`` (story 02 /
ALP-666) on ``request.app.state.foreign_reader_session_factory``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.expression import ColumnElement

from alphamind.state.tables.invocations import InvocationRow

__all__ = ["build_history_router"]

# ---------------------------------------------------------------------------
# Status literals
# ---------------------------------------------------------------------------

_STATUS_COMPLETED = "completed"
_STATUS_PARTIAL = "partial"
_STATUS_FAILED = "failed"
_VALID_STATUSES = frozenset({_STATUS_COMPLETED, _STATUS_PARTIAL, _STATUS_FAILED})

# ---------------------------------------------------------------------------
# Pydantic response models (boundary types per ALP-128 invariant)
# ---------------------------------------------------------------------------


class RunRow(BaseModel):
    """One row in the run history list.

    All fields sourced from the ``invocations`` table; numeric summaries
    extracted from the phase-completion JSON blobs.
    """

    invocation_id: str
    started_at: str
    """ISO-8601 timestamp from ``start_at``."""
    ended_at: str | None
    """Latest completed-phase timestamp; null if both phases incomplete."""
    duration_seconds: float | None
    """Wall-clock seconds from ``start_at`` to ``ended_at``."""
    run_type: str
    """``trigger_source`` value (e.g. ``pre_open``)."""
    status: str
    """Derived: ``completed`` | ``partial`` | ``failed``."""
    commands_submitted: int
    """From ``command_execution_summary_json``; 0 if absent."""
    commands_rejected: int
    """From ``command_execution_summary_json``; 0 if absent."""
    abort_reason: str | None
    """From ``snapshot_metadata_json``; null for most runs."""


class RunHistoryPage(BaseModel):
    """Paginated response envelope for the run history list."""

    items: list[RunRow]
    total: int = Field(description="Total matching rows (pre-pagination).")
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Internal: derive status + parse summary JSON
# ---------------------------------------------------------------------------


def _derive_status(phase1: str | None, phase2: str | None) -> str:
    """Derive the human-readable status from phase completion timestamps."""
    if phase2 is not None:
        return _STATUS_COMPLETED
    if phase1 is not None:
        return _STATUS_PARTIAL
    return _STATUS_FAILED


def _extract_int(json_str: str | None, key: str) -> int:
    """Extract an integer from a JSON blob; return 0 on any failure."""
    if json_str is None:
        return 0
    try:
        data = json.loads(json_str)
        return int(data.get(key, 0))
    except (ValueError, TypeError, AttributeError):
        return 0


def _compute_duration(start: str, end: str) -> float | None:
    """Return the duration in seconds between two ISO-8601 timestamps."""
    try:
        t0 = datetime.fromisoformat(start.rstrip("Z"))
        t1 = datetime.fromisoformat(end.rstrip("Z"))
        return (t1 - t0).total_seconds()
    except (ValueError, TypeError):
        return None


def _extract_abort_reason(snapshot_json: str | None) -> str | None:
    """Extract ``abort_reason`` from ``snapshot_metadata_json``."""
    if not snapshot_json:
        return None
    try:
        meta = json.loads(snapshot_json)
        if isinstance(meta, dict):
            return meta.get("abort_reason")
    except (ValueError, TypeError):
        pass
    return None


def _row_to_run_row(row: InvocationRow) -> RunRow:
    """Convert an ORM row to a ``RunRow`` response model."""
    status = _derive_status(row.phase1_completed_at, row.phase2_completed_at)
    ended_at = row.phase2_completed_at or row.phase1_completed_at
    duration_seconds = _compute_duration(row.start_at, ended_at) if ended_at else None
    return RunRow(
        invocation_id=row.invocation_id,
        started_at=row.start_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        run_type=row.trigger_source,
        status=status,
        commands_submitted=_extract_int(row.command_execution_summary_json, "commands_submitted"),
        commands_rejected=_extract_int(row.command_execution_summary_json, "commands_rejected"),
        abort_reason=_extract_abort_reason(row.snapshot_metadata_json),
    )


# ---------------------------------------------------------------------------
# Internal: filter dataclass + query builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RunHistoryFilters:
    """Validated filter values extracted from query params."""

    date_from: str | None
    date_to: str | None
    run_type: str | None
    statuses: frozenset[str]
    """Empty means no status filter (all statuses returned)."""
    commands_gt: int | None
    has_errors: bool | None
    page: int
    page_size: int


def _status_predicates(
    statuses: frozenset[str],
) -> list[ColumnElement[bool]]:
    """Build phase-timestamp predicates for the given status set."""
    predicates: list[ColumnElement[bool]] = []
    if _STATUS_COMPLETED in statuses:
        predicates.append(InvocationRow.phase2_completed_at.is_not(None))
    if _STATUS_PARTIAL in statuses:
        predicates.append(
            InvocationRow.phase1_completed_at.is_not(None)
            & InvocationRow.phase2_completed_at.is_(None)
        )
    if _STATUS_FAILED in statuses:
        predicates.append(
            InvocationRow.phase1_completed_at.is_(None)
            & InvocationRow.phase2_completed_at.is_(None)
        )
    return predicates


def _build_base_query(
    filters: _RunHistoryFilters,
) -> Select[tuple[InvocationRow]]:
    """Return a SELECT over ``invocations`` with all filter clauses applied.

    Does NOT apply ORDER BY or LIMIT/OFFSET — callers add those for the
    count vs. data variants.
    """
    stmt: Select[tuple[InvocationRow]] = select(InvocationRow)

    if filters.date_from is not None:
        stmt = stmt.where(InvocationRow.start_at >= filters.date_from)
    if filters.date_to is not None:
        stmt = stmt.where(InvocationRow.start_at <= filters.date_to)
    if filters.run_type is not None:
        stmt = stmt.where(InvocationRow.trigger_source == filters.run_type)

    if filters.statuses:
        preds = _status_predicates(filters.statuses)
        if preds:
            stmt = stmt.where(or_(*preds))

    if filters.has_errors is True:
        rejected_expr = func.json_extract(
            InvocationRow.command_execution_summary_json, "$.commands_rejected"
        )
        stmt = stmt.where(rejected_expr > 0)
    elif filters.has_errors is False:
        rejected_expr = func.json_extract(
            InvocationRow.command_execution_summary_json, "$.commands_rejected"
        )
        stmt = stmt.where(rejected_expr.is_(None) | (rejected_expr == 0))

    if filters.commands_gt is not None:
        submitted_expr = func.json_extract(
            InvocationRow.command_execution_summary_json, "$.commands_submitted"
        )
        stmt = stmt.where(submitted_expr > filters.commands_gt)

    return stmt


async def _fetch_page(
    session: AsyncSession,
    filters: _RunHistoryFilters,
) -> tuple[list[RunRow], int]:
    """Run count + paginated data queries; return (items, total)."""
    base = _build_base_query(filters)

    count_result = await session.execute(select(func.count()).select_from(base.subquery()))
    total: int = count_result.scalar_one()

    offset = (filters.page - 1) * filters.page_size
    data_stmt = base.order_by(InvocationRow.start_at.desc()).offset(offset).limit(filters.page_size)
    rows_result = await session.execute(data_stmt)
    rows: Sequence[InvocationRow] = rows_result.scalars().all()
    return [_row_to_run_row(r) for r in rows], total


# ---------------------------------------------------------------------------
# Dependency: reader session factory
# ---------------------------------------------------------------------------


def _reader_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.foreign_reader_session_factory
    return factory


# ---------------------------------------------------------------------------
# Query-param parsing helpers
# ---------------------------------------------------------------------------


def _parse_statuses(raw: list[str]) -> frozenset[str]:
    """Normalise and validate the ``status`` multi-value query param."""
    statuses: set[str] = set()
    for s in raw:
        for part in s.split(","):
            part = part.strip().lower()
            if part and part in _VALID_STATUSES:
                statuses.add(part)
    return frozenset(statuses)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_history_router() -> APIRouter:
    """Return the ``/api/views/history`` router.

    Story 05d will extend the returned router or add a sibling router;
    keep this factory clean so that adding ``invocation_detail_*``
    endpoints in 05d does not conflict with the ``run_history_*``
    endpoints defined here.
    """
    router = APIRouter(tags=["history"])

    @router.get(
        "/runs",
        response_model=RunHistoryPage,
        summary="Paginated, filterable list of past pipeline invocations.",
    )
    async def run_history_list(  # noqa: PLR0913 — FastAPI route; each query param is a distinct filter dimension
        date_from: Annotated[str | None, Query()] = None,
        date_to: Annotated[str | None, Query()] = None,
        run_type: Annotated[str | None, Query()] = None,
        status: Annotated[
            list[str],
            Query(description="completed|failed|partial; repeatable."),
        ] = [],  # noqa: B006
        commands_gt: Annotated[int | None, Query(ge=0)] = None,
        has_errors: Annotated[bool | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 50,
        reader: Annotated[async_sessionmaker[AsyncSession], Depends(_reader_factory)] = ...,  # type: ignore[assignment]
    ) -> RunHistoryPage:
        """Paginated list of invocations; all filters are AND-combined.

        ``status`` can be repeated (``?status=failed&status=partial``)
        or comma-separated (``?status=failed,partial``).
        """
        filters = _RunHistoryFilters(
            date_from=date_from,
            date_to=date_to,
            run_type=run_type,
            statuses=_parse_statuses(status),
            commands_gt=commands_gt,
            has_errors=has_errors,
            page=page,
            page_size=page_size,
        )
        async with reader() as session:
            items, total = await _fetch_page(session, filters)
        return RunHistoryPage(items=items, total=total, page=page, page_size=page_size)

    @router.get(
        "/runs/preset/failure-log",
        response_model=RunHistoryPage,
        summary="Failure and abort log — status ∈ {failed, partial}.",
    )
    async def run_history_failure_log(
        date_from: Annotated[str | None, Query()] = None,
        date_to: Annotated[str | None, Query()] = None,
        run_type: Annotated[str | None, Query()] = None,
        commands_gt: Annotated[int | None, Query(ge=0)] = None,
        has_errors: Annotated[bool | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 50,
        reader: Annotated[async_sessionmaker[AsyncSession], Depends(_reader_factory)] = ...,  # type: ignore[assignment]
    ) -> RunHistoryPage:
        """Convenience preset: ``status ∈ {failed, partial}`` pre-applied.

        Same query-param shape as ``GET /runs`` minus the ``status``
        filter (fixed to ``{failed, partial}``).
        """
        filters = _RunHistoryFilters(
            date_from=date_from,
            date_to=date_to,
            run_type=run_type,
            statuses=frozenset({_STATUS_FAILED, _STATUS_PARTIAL}),
            commands_gt=commands_gt,
            has_errors=has_errors,
            page=page,
            page_size=page_size,
        )
        async with reader() as session:
            items, total = await _fetch_page(session, filters)
        return RunHistoryPage(items=items, total=total, page=page, page_size=page_size)

    return router
