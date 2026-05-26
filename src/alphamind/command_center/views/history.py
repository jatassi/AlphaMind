"""Run-history view endpoints (story 05c / ALP-673).

Provides:

* ``GET /api/views/history/runs`` -- paginated, filterable list of past
  pipeline invocations. Filter dimensions: date range, run type, status,
  command-count-greater-than, has-errors. Default sort: started_at DESC.
  Page size capped at 100.
* ``GET /api/views/history/runs/preset/failure-log`` -- convenience preset
  with ``status in {failed, partial}`` pre-applied; accepts the same
  query-param shape minus the ``status`` filter.

Reads via :func:`alphamind.command_center.persistence.session.build_foreign_reader_session_factory`
(the read-only foreign-table session factory from story 02 / ALP-666).

Pydantic boundary: request query params decoded into :class:`RunHistoryParams`
/ :class:`FailureLogParams`; response shaped as :class:`RunHistoryPage`
containing a list of :class:`RunRow` items.

Internal helpers operate on plain Python dataclasses and SQLAlchemy core;
no ORM writes cross this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.state.tables.invocations import InvocationRow

__all__ = ["build_history_router"]

# ---------------------------------------------------------------------------
# Pydantic boundary models
# ---------------------------------------------------------------------------

_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 20


class RunRow(BaseModel):
    """One row in the run-history page."""

    invocation_id: str
    started_at: str
    ended_at: str | None
    duration_seconds: float | None
    run_type: str
    status: str
    command_count: int
    rejection_count: int
    abort_reason: str | None


class RunHistoryPage(BaseModel):
    """Paginated response for /api/views/history/runs."""

    items: list[RunRow]
    page: int
    page_size: int
    total: int


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RunFilter:
    """Compiled filter state for the run-history query.

    Constructed once per request from the validated query params;
    passed to :func:`_query_runs` so the query helper has no FastAPI
    surface dependency.
    """

    date_from: str | None
    date_to: str | None
    run_type: str | None
    statuses: tuple[str, ...]
    commands_gt: int | None
    has_errors: bool | None
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _parse_command_count(summary_json: str | None) -> int:
    """Extract the command count from ``command_execution_summary_json``."""
    if not summary_json:
        return 0
    try:
        data = json.loads(summary_json)
        return int(data.get("command_count", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def _parse_rejection_count(summary_json: str | None) -> int:
    """Extract the rejection count from ``command_execution_summary_json``."""
    if not summary_json:
        return 0
    try:
        data = json.loads(summary_json)
        return int(data.get("rejection_count", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def _parse_abort_reason(summary_json: str | None) -> str | None:
    """Extract the abort reason from ``command_execution_summary_json``."""
    if not summary_json:
        return None
    try:
        data = json.loads(summary_json)
        reason = data.get("abort_reason")
        return str(reason) if reason is not None else None
    except (json.JSONDecodeError, TypeError):
        return None


def _derive_status(row: InvocationRow) -> str:
    """Derive a run status from the invocation row fields.

    The ``invocations`` table has no ``status`` column; status is inferred
    from the completion timestamps and the ``command_execution_summary_json``
    abort_reason field:

    * ``failed`` -- abort_reason is set and ``phase2_completed_at`` is NULL.
    * ``partial`` -- abort_reason is set and ``phase2_completed_at`` is set.
    * ``completed`` -- ``phase2_completed_at`` is set and no abort_reason.
    * ``running`` -- no abort_reason and ``phase2_completed_at`` is NULL.
    """
    abort_reason = _parse_abort_reason(row.command_execution_summary_json)
    if abort_reason:
        if row.phase2_completed_at is not None:
            return "partial"
        return "failed"
    if row.phase2_completed_at is not None:
        return "completed"
    return "running"


def _has_errors(row: InvocationRow) -> bool:
    """Return True if the invocation has any errors or rejections."""
    return _parse_rejection_count(row.command_execution_summary_json) > 0 or bool(
        _parse_abort_reason(row.command_execution_summary_json)
    )


def _duration_seconds(row: InvocationRow) -> float | None:
    """Compute wall-clock duration in seconds from ISO timestamps."""
    if row.phase2_completed_at is None:
        return None
    try:
        start = datetime.fromisoformat(row.start_at)
        end = datetime.fromisoformat(row.phase2_completed_at)
        return (end - start).total_seconds()
    except (ValueError, TypeError):
        return None


def _row_to_run_row(row: InvocationRow) -> RunRow:
    """Map an :class:`InvocationRow` ORM instance to a :class:`RunRow`."""
    return RunRow(
        invocation_id=row.invocation_id,
        started_at=row.start_at,
        ended_at=row.phase2_completed_at,
        duration_seconds=_duration_seconds(row),
        run_type=row.trigger_source,
        status=_derive_status(row),
        command_count=_parse_command_count(row.command_execution_summary_json),
        rejection_count=_parse_rejection_count(row.command_execution_summary_json),
        abort_reason=_parse_abort_reason(row.command_execution_summary_json),
    )


async def _query_runs(
    session: AsyncSession,
    filt: _RunFilter,
) -> RunHistoryPage:
    """Execute the filtered + paginated query and return the page."""
    stmt = select(InvocationRow)

    conditions = []
    if filt.date_from:
        conditions.append(InvocationRow.start_at >= filt.date_from)
    if filt.date_to:
        conditions.append(InvocationRow.start_at <= filt.date_to)
    if filt.run_type:
        conditions.append(InvocationRow.trigger_source == filt.run_type)
    if conditions:
        stmt = stmt.where(and_(*conditions))

    result = await session.execute(stmt)
    all_rows: list[InvocationRow] = list(result.scalars().all())

    # Post-filter on derived fields (status, command count, has_errors)
    # because these are computed from JSON columns, not raw SQL columns.
    filtered: list[InvocationRow] = []
    for row in all_rows:
        if filt.statuses and _derive_status(row) not in filt.statuses:
            continue
        if filt.commands_gt is not None and (
            _parse_command_count(row.command_execution_summary_json) <= filt.commands_gt
        ):
            continue
        if filt.has_errors is not None and _has_errors(row) != filt.has_errors:
            continue
        filtered.append(row)

    # Sort DESC by started_at.
    filtered.sort(key=lambda r: r.start_at, reverse=True)

    total = len(filtered)
    offset = (filt.page - 1) * filt.page_size
    page_rows = filtered[offset : offset + filt.page_size]
    items = [_row_to_run_row(r) for r in page_rows]
    return RunHistoryPage(
        items=items,
        page=filt.page,
        page_size=filt.page_size,
        total=total,
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def _get_foreign_reader(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.foreign_reader_session_factory  # type: ignore[no-any-return]


_ForeignReader = Annotated[async_sessionmaker[AsyncSession], Depends(_get_foreign_reader)]


class _BaseQueryParams:
    """Shared query parameters for run-history endpoints.

    Used as a ``Depends``-injectable class so route handlers stay under
    the 8-param limit (PLR0913).
    """

    def __init__(
        self,
        date_from: Annotated[str | None, Query()] = None,
        date_to: Annotated[str | None, Query()] = None,
        run_type: Annotated[str | None, Query()] = None,
        commands_gt: Annotated[int | None, Query()] = None,
        has_errors: Annotated[bool | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = _DEFAULT_PAGE_SIZE,
    ) -> None:
        self.date_from = date_from
        self.date_to = date_to
        self.run_type = run_type
        self.commands_gt = commands_gt
        self.has_errors = has_errors
        self.page = page
        self.page_size = page_size


_BaseParams = Annotated[_BaseQueryParams, Depends(_BaseQueryParams)]


# ---------------------------------------------------------------------------
# Router builder
# ---------------------------------------------------------------------------


def build_history_router() -> APIRouter:
    """Return the history-view :class:`APIRouter`.

    Included in :func:`alphamind.command_center.app.build_app` under
    ``/api/views/history`` (story 05c / ALP-673).
    """
    router = APIRouter()

    @router.get("/runs/preset/failure-log", response_model=RunHistoryPage)
    async def failure_log(
        factory: _ForeignReader,
        params: _BaseParams,
    ) -> RunHistoryPage:
        """Failure-log preset: status in {failed, partial} pre-applied."""
        filt = _RunFilter(
            date_from=params.date_from,
            date_to=params.date_to,
            run_type=params.run_type,
            statuses=("failed", "partial"),
            commands_gt=params.commands_gt,
            has_errors=params.has_errors,
            page=params.page,
            page_size=params.page_size,
        )
        async with factory() as session:
            return await _query_runs(session, filt)

    @router.get("/runs", response_model=RunHistoryPage)
    async def run_history(
        factory: _ForeignReader,
        params: _BaseParams,
        status: Annotated[list[str] | None, Query()] = None,
    ) -> RunHistoryPage:
        """Paginated, filterable list of past pipeline invocations."""
        filt = _RunFilter(
            date_from=params.date_from,
            date_to=params.date_to,
            run_type=params.run_type,
            statuses=tuple(status) if status else (),
            commands_gt=params.commands_gt,
            has_errors=params.has_errors,
            page=params.page,
            page_size=params.page_size,
        )
        async with factory() as session:
            return await _query_runs(session, filt)

    return router
