"""View B-3 -- Activity log explorer (story 05e / ALP-675).

Three endpoints:

* ``GET /api/views/activity-log`` -- paginated activity_log rows with
  multi-select filter dimensions. Reads via ``foreign_reader_session``.
  Page size capped at 200.

* ``GET /api/views/activity-log/event-types`` -- returns the EventType
  enum values for filter chip population.

* ``GET /api/views/activity-log/saved-filters`` -- returns built-in
  saved filters; v1 ships one: "Operator actions" with
  ``source=operator_console``.

Pydantic models live in this module; they sit at the FastAPI boundary
(request query params + response bodies). Internal logic uses plain
SQLAlchemy core queries against the ``activity_log`` table, which is
reached via the ``foreign_reader_session_factory`` on ``app.state``
(story 02 / ALP-666).

Auth gating: every endpoint requires ``Depends(current_session)`` -- the
auth dependency validates the session cookie and raises 401 if invalid.
No CSRF is needed because these are read-only GET endpoints.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.command_center.auth.dependencies import current_session
from alphamind.portfolio_state.events.types import EventSource, EventType

__all__ = ["build_activity_log_router"]

log = logging.getLogger(__name__)

_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Response models (Pydantic at boundaries)
# ---------------------------------------------------------------------------


class ActivityLogRow(BaseModel):
    """One row in the paginated activity log response."""

    entry_id: str
    invocation_id: str
    entry_at: str
    event_type: str
    event_group: str
    position_id: str | None
    order_id: str | None
    thesis_id: str | None
    envelope_id: str | None
    source: str
    detail_json: str
    # Typed decoded detail -- None when decoding fails or type is unknown.
    detail: dict[str, Any] | None


class ActivityLogPage(BaseModel):
    """Paginated response envelope for activity log rows."""

    rows: list[ActivityLogRow]
    total: int
    page: int
    page_size: int
    has_more: bool


class EventTypesResponse(BaseModel):
    """Response for GET /event-types -- full EventType enum catalog."""

    event_types: list[str]


class SavedFilter(BaseModel):
    """One built-in saved filter preset."""

    id: str
    label: str
    description: str
    filters: dict[str, list[str] | str]


class SavedFiltersResponse(BaseModel):
    """Response for GET /saved-filters -- built-in preset list."""

    saved_filters: list[SavedFilter]


# ---------------------------------------------------------------------------
# Built-in saved filters (v1: Operator actions only)
# ---------------------------------------------------------------------------

_SAVED_FILTERS: list[SavedFilter] = [
    SavedFilter(
        id="operator_actions",
        label="Operator actions",
        description="Activity log entries from the operator console",
        filters={"source": [EventSource.OPERATOR_CONSOLE.value]},
    ),
]


# ---------------------------------------------------------------------------
# Internal filter/pagination params dataclass (avoids PLR0913 in functions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActivityLogFilters:
    """Bundled filter + pagination params for the activity log query."""

    event_type: list[str] | None
    invocation_id: str | None
    position_id: str | None
    thesis_id: str | None
    order_id: str | None
    source: list[str] | None
    time_from: str | None
    time_to: str | None
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _parse_detail(detail_json: str) -> dict[str, Any] | None:
    """Attempt to parse the detail_json column as a dict.

    Returns None if the value is not valid JSON or not a dict; callers
    render the raw string in that case. The detail column is always
    valid JSON in well-formed rows (written by ``encode_detail``), but
    we fail-open here so a corrupt row doesn't crash the view.
    """
    try:
        parsed = json.loads(detail_json)
    except (json.JSONDecodeError, ValueError):
        return None
    else:
        return parsed if isinstance(parsed, dict) else None


def _row_to_model(row: Any) -> ActivityLogRow:
    """Map a SQLAlchemy Row to :class:`ActivityLogRow`."""
    # envelope_id is not a column on the table -- it lives inside detail_json
    # for PM_DECISION / ENVELOPE_REJECTED / ENVELOPE_PARSE_FAILED events.
    # We extract it here for the entity-chip deep-link so the frontend can
    # navigate without parsing detail_json client-side.
    detail = _parse_detail(row.detail_json)
    envelope_id: str | None = None
    if detail is not None:
        raw_eid = detail.get("envelope_id") or detail.get("pm_envelope_id")
        envelope_id = raw_eid if isinstance(raw_eid, str) else None

    return ActivityLogRow(
        entry_id=row.entry_id,
        invocation_id=row.invocation_id,
        entry_at=row.entry_at,
        event_type=row.event_type,
        event_group=row.event_group,
        position_id=row.position_id,
        order_id=row.order_id,
        thesis_id=row.thesis_id,
        envelope_id=envelope_id,
        source=row.source,
        detail_json=row.detail_json,
        detail=detail,
    )


def _apply_multi_select(
    column: str,
    prefix: str,
    values: list[str],
    where_clauses: list[str],
    params: dict[str, Any],
) -> None:
    """Append an IN clause + numbered params for a multi-select filter."""
    placeholders = ", ".join(f":{prefix}_{i}" for i in range(len(values)))
    where_clauses.append(f"{column} IN ({placeholders})")
    for i, v in enumerate(values):
        params[f"{prefix}_{i}"] = v


def _apply_scalar_filters(
    filters: _ActivityLogFilters,
    where_clauses: list[str],
    params: dict[str, Any],
) -> None:
    """Append scalar equality / range clauses for the filter bundle."""
    if filters.invocation_id is not None:
        where_clauses.append("invocation_id = :invocation_id")
        params["invocation_id"] = filters.invocation_id
    if filters.position_id is not None:
        where_clauses.append("position_id = :position_id")
        params["position_id"] = filters.position_id
    if filters.thesis_id is not None:
        where_clauses.append("thesis_id = :thesis_id")
        params["thesis_id"] = filters.thesis_id
    if filters.order_id is not None:
        where_clauses.append("order_id = :order_id")
        params["order_id"] = filters.order_id
    if filters.time_from is not None:
        where_clauses.append("entry_at >= :time_from")
        params["time_from"] = filters.time_from
    if filters.time_to is not None:
        where_clauses.append("entry_at <= :time_to")
        params["time_to"] = filters.time_to


def _build_where(
    filters: _ActivityLogFilters,
) -> tuple[str, dict[str, Any]]:
    """Return ``(where_sql, params)`` from the filter bundle.

    where_sql is the complete WHERE clause string (empty string if no
    filters are active). params is the bound-parameter dict to pass to
    sqlalchemy ``text()`` execution.
    """
    where_clauses: list[str] = []
    params: dict[str, Any] = {}

    if filters.event_type:
        _apply_multi_select("event_type", "et", filters.event_type, where_clauses, params)
    if filters.source:
        _apply_multi_select("source", "src", filters.source, where_clauses, params)
    _apply_scalar_filters(filters, where_clauses, params)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    return where_sql, params


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_activity_log_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` for the activity log view.

    Mounted under ``/api/views/activity-log`` by
    :func:`alphamind.command_center.app.build_app`.
    """
    router = APIRouter(tags=["views-activity-log"])

    @router.get(
        "/event-types",
        response_model=EventTypesResponse,
        summary="List activity log event types",
    )
    async def list_event_types(
        _session_id: Annotated[str, Depends(current_session)],
    ) -> EventTypesResponse:
        """Return the full EventType enum catalog for filter chip population.

        The list is derived from the
        :class:`~alphamind.portfolio_state.events.types.EventType` enum at
        request time -- adding a new event type to the catalog automatically
        surfaces here without a separate migration.
        """
        return EventTypesResponse(
            event_types=[et.value for et in EventType],
        )

    @router.get(
        "/saved-filters",
        response_model=SavedFiltersResponse,
        summary="List saved activity log filter presets",
    )
    async def list_saved_filters(
        _session_id: Annotated[str, Depends(current_session)],
    ) -> SavedFiltersResponse:
        """Return built-in saved filter presets.

        v1 ships one preset: "Operator actions" (``source=operator_console``).
        Custom user-saved filters are deferred to the F-group stories per the
        parent issue scope.
        """
        return SavedFiltersResponse(saved_filters=_SAVED_FILTERS)

    @router.get(
        "",
        response_model=ActivityLogPage,
        summary="Paginated activity log query",
    )
    async def query_activity_log(  # noqa: PLR0913 -- FastAPI route: each filter is a distinct query param
        request: Request,
        _session_id: Annotated[str, Depends(current_session)],
        event_type: Annotated[list[str] | None, Query()] = None,
        invocation_id: Annotated[str | None, Query()] = None,
        position_id: Annotated[str | None, Query()] = None,
        thesis_id: Annotated[str | None, Query()] = None,
        order_id: Annotated[str | None, Query()] = None,
        source: Annotated[list[str] | None, Query()] = None,
        time_from: Annotated[str | None, Query()] = None,
        time_to: Annotated[str | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = _DEFAULT_PAGE_SIZE,
    ) -> ActivityLogPage:
        """Return a paginated page of activity_log rows matching the filters.

        All filter dimensions are optional; omitted filters are not applied.
        Multi-select dimensions (``event_type``, ``source``) accept multiple
        query-string values
        (``?event_type=POSITION_OPENED&event_type=ORDER_FILLED``).

        Rows are ordered by ``entry_at`` descending (newest first), then
        ``entry_id`` descending for tie-breaking within the same second.

        Page size is capped at 200 server-side regardless of the ``page_size``
        query param -- the ``le=200`` validator in the FastAPI declaration
        enforces this at the HTTP layer.
        """
        filters = _ActivityLogFilters(
            event_type=event_type,
            invocation_id=invocation_id,
            position_id=position_id,
            thesis_id=thesis_id,
            order_id=order_id,
            source=source,
            time_from=time_from,
            time_to=time_to,
            page=page,
            page_size=page_size,
        )
        factory = request.app.state.foreign_reader_session_factory
        async with factory() as db_session:
            return await _execute_query(db_session, filters)

    return router


# ---------------------------------------------------------------------------
# Query implementation (separated for testability)
# ---------------------------------------------------------------------------


async def _execute_query(
    db_session: AsyncSession,
    filters: _ActivityLogFilters,
) -> ActivityLogPage:
    """Execute the filtered + paginated query against the activity_log table.

    Separated from the route handler so tests can call it directly with
    an in-memory session without going through the full FastAPI request
    lifecycle. All filter/pagination logic lives here.
    """
    where_sql, params = _build_where(filters)

    # COUNT query for the total (needed for the page envelope).
    count_sql = text(f"SELECT COUNT(*) FROM activity_log {where_sql}")
    count_result = await db_session.execute(count_sql, params)
    total: int = count_result.scalar_one()

    # Data query: paginated, newest-first.
    offset = (filters.page - 1) * filters.page_size
    data_sql = text(
        f"SELECT entry_id, invocation_id, entry_at, event_type, event_group, "
        f"position_id, order_id, thesis_id, source, detail_json "
        f"FROM activity_log {where_sql} "
        f"ORDER BY entry_at DESC, entry_id DESC "
        f"LIMIT :limit OFFSET :offset"
    )
    data_params = {**params, "limit": filters.page_size, "offset": offset}
    data_result = await db_session.execute(data_sql, data_params)
    rows = [_row_to_model(r) for r in data_result.fetchall()]

    return ActivityLogPage(
        rows=rows,
        total=total,
        page=filters.page,
        page_size=filters.page_size,
        has_more=(offset + len(rows)) < total,
    )
