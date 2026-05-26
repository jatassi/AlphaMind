"""Activity log explorer view (story 05e / ALP-675).

Exposes three read-only endpoints under ``/api/views/activity-log``:

* ``GET /api/views/activity-log`` — paginated activity_log rows with
  multi-select filter dimensions (event_type, invocation_id,
  position_id, thesis_id, order_id, source, time_from, time_to).
  Reads via the ``foreign_reader_session`` factory wired onto
  ``app.state`` by the lifespan. Page size is capped at 200.

* ``GET /api/views/activity-log/event-types`` — returns the
  :class:`~alphamind.portfolio_state.events.types.EventType` enum
  values for filter chip population on the frontend.

* ``GET /api/views/activity-log/saved-filters`` — returns built-in
  saved filters; v1 ships one: "Operator actions" with
  ``source=operator_console``.

All three endpoints are mounted under the ``/api/views`` prefix by
:func:`alphamind.command_center.app.build_app`; this module exports only
:func:`build_activity_log_router` so the composition root stays the sole
wiring point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.portfolio_state.events.types import EventSource, EventType
from alphamind.state.tables.activity_log import ActivityLogRow

__all__ = ["build_activity_log_router"]

_PAGE_SIZE_MAX = 200
_PAGE_SIZE_DEFAULT = 50


# ---------------------------------------------------------------------------
# Response models (Pydantic at the HTTP boundary)
# ---------------------------------------------------------------------------


class ActivityLogRowResponse(BaseModel):
    """One row from the ``activity_log`` table as returned by the API.

    ``detail_json`` is the raw JSON string from the DB — the frontend
    decodes it using the event-type-to-detail mapping shipped alongside.
    """

    entry_id: str
    invocation_id: str
    entry_at: str
    event_type: str
    event_group: str
    position_id: str | None
    order_id: str | None
    thesis_id: str | None
    source: str
    detail_json: str


class ActivityLogPage(BaseModel):
    """Paginated activity-log response."""

    rows: list[ActivityLogRowResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


class EventTypesResponse(BaseModel):
    """All EventType enum values for filter chip population."""

    event_types: list[str]


class SavedFilter(BaseModel):
    """One named saved-filter preset."""

    name: str
    description: str
    params: dict[str, list[str]]


class SavedFiltersResponse(BaseModel):
    """Built-in saved-filter presets."""

    saved_filters: list[SavedFilter]


# ---------------------------------------------------------------------------
# Dependency — extract the foreign_reader session factory from app.state
# ---------------------------------------------------------------------------


def _get_reader_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Pull the foreign_reader_session_factory off ``app.state``."""
    return request.app.state.foreign_reader_session_factory  # type: ignore[no-any-return]


ReaderFactory = Annotated[async_sessionmaker[AsyncSession], Depends(_get_reader_factory)]


# ---------------------------------------------------------------------------
# Query-parameter dependencies (split to respect PLR0913 ≤ 8 args each)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityFilters:
    """Filter dimensions that narrow by entity ID or subsystem.

    Collected by :func:`entity_filters_dep` so the handler doesn't
    exceed 8 function parameters (PLR0913).
    """

    event_type: list[str]
    invocation_id: str | None
    position_id: str | None
    thesis_id: str | None
    order_id: str | None
    source: list[str]
    time_from: str | None
    time_to: str | None


@dataclass(frozen=True, slots=True)
class PaginationParams:
    """Pagination parameters extracted by :func:`pagination_dep`."""

    page: int
    page_size: int


def entity_filters_dep(
    event_type: Annotated[list[str] | None, Query()] = None,
    invocation_id: Annotated[str | None, Query()] = None,
    position_id: Annotated[str | None, Query()] = None,
    thesis_id: Annotated[str | None, Query()] = None,
    order_id: Annotated[str | None, Query()] = None,
    source: Annotated[list[str] | None, Query()] = None,
    time_from: Annotated[str | None, Query()] = None,
    time_to: Annotated[str | None, Query()] = None,
) -> EntityFilters:
    """FastAPI dependency that materialises :class:`EntityFilters`."""
    return EntityFilters(
        event_type=event_type or [],
        invocation_id=invocation_id,
        position_id=position_id,
        thesis_id=thesis_id,
        order_id=order_id,
        source=source or [],
        time_from=time_from,
        time_to=time_to,
    )


def pagination_dep(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=_PAGE_SIZE_MAX)] = _PAGE_SIZE_DEFAULT,
) -> PaginationParams:
    """FastAPI dependency that materialises :class:`PaginationParams`."""
    return PaginationParams(page=page, page_size=page_size)


EntityFiltersDepend = Annotated[EntityFilters, Depends(entity_filters_dep)]
PaginationDepend = Annotated[PaginationParams, Depends(pagination_dep)]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_activity_log_router() -> APIRouter:
    """Construct the activity-log view router.

    The returned router is mounted at ``/api/views/activity-log`` by
    :func:`alphamind.command_center.app.build_app`.
    """
    router = APIRouter(tags=["views"])

    @router.get("/event-types", response_model=EventTypesResponse)
    async def get_event_types() -> EventTypesResponse:
        """Return all EventType enum values for filter chip population."""
        return EventTypesResponse(event_types=[e.value for e in EventType])

    @router.get("/saved-filters", response_model=SavedFiltersResponse)
    async def get_saved_filters() -> SavedFiltersResponse:
        """Return built-in saved-filter presets.

        v1 ships one preset: "Operator actions" (source=operator_console).
        Custom saved filters defer to the F-group (``saved_queries`` table).
        """
        return SavedFiltersResponse(
            saved_filters=[
                SavedFilter(
                    name="Operator actions",
                    description="Events originating from the operator console",
                    params={"source": [EventSource.OPERATOR_CONSOLE.value]},
                )
            ]
        )

    @router.get("", response_model=ActivityLogPage)
    async def get_activity_log(
        reader_factory: ReaderFactory,
        entity_filters: EntityFiltersDepend,
        pagination: PaginationDepend,
    ) -> ActivityLogPage:
        """Return paginated activity_log rows with multi-select filters.

        Filter dimensions are split across :class:`EntityFilters` (entity IDs,
        event type, source, time range) and :class:`PaginationParams` (page,
        page_size). Rows are ordered newest-first (``entry_at DESC``).
        """
        clauses = _build_where(entity_filters)
        offset = (pagination.page - 1) * pagination.page_size

        async with reader_factory() as session:
            total, rows = await _query_page(session, clauses, offset, pagination.page_size)

        return ActivityLogPage(
            rows=rows,
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
            has_more=(offset + len(rows)) < total,
        )

    return router


# ---------------------------------------------------------------------------
# Internal query helpers
# ---------------------------------------------------------------------------


def _build_where(filters: EntityFilters) -> list[object]:
    """Build SQLAlchemy WHERE clauses from the resolved filter object."""
    clauses: list[object] = []
    if filters.event_type:
        clauses.append(ActivityLogRow.event_type.in_(filters.event_type))
    if filters.invocation_id:
        clauses.append(ActivityLogRow.invocation_id == filters.invocation_id)
    if filters.position_id:
        clauses.append(ActivityLogRow.position_id == filters.position_id)
    if filters.thesis_id:
        clauses.append(ActivityLogRow.thesis_id == filters.thesis_id)
    if filters.order_id:
        clauses.append(ActivityLogRow.order_id == filters.order_id)
    if filters.source:
        clauses.append(ActivityLogRow.source.in_(filters.source))
    if filters.time_from:
        clauses.append(ActivityLogRow.entry_at >= filters.time_from)
    if filters.time_to:
        clauses.append(ActivityLogRow.entry_at <= filters.time_to)
    return clauses


async def _query_page(
    session: AsyncSession,
    clauses: list[object],
    offset: int,
    page_size: int,
) -> tuple[int, list[ActivityLogRowResponse]]:
    """Execute COUNT + data SELECT, return (total, rows)."""
    where_clause = and_(*clauses) if clauses else True  # type: ignore[arg-type]

    count_result = await session.execute(
        select(ActivityLogRow).where(where_clause)  # type: ignore[arg-type]
    )
    total = len(count_result.scalars().all())

    data_result = await session.execute(
        select(ActivityLogRow)
        .where(where_clause)  # type: ignore[arg-type]
        .order_by(ActivityLogRow.entry_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    return total, [_to_response(r) for r in data_result.scalars().all()]


def _to_response(row: ActivityLogRow) -> ActivityLogRowResponse:
    """Convert a SQLAlchemy ORM row to the API response model."""
    return ActivityLogRowResponse(
        entry_id=row.entry_id,
        invocation_id=row.invocation_id,
        entry_at=row.entry_at,
        event_type=row.event_type,
        event_group=row.event_group,
        position_id=row.position_id,
        order_id=row.order_id,
        thesis_id=row.thesis_id,
        source=row.source,
        detail_json=row.detail_json,
    )
