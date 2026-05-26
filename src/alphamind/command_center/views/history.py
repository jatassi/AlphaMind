"""Run history view — ``GET /api/views/history/runs`` (stories 05c/05d).

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

Story 05d (ALP-674) extends this module with per-invocation detail
endpoints (``invocation_detail_*`` prefix), the archive-file streaming
endpoint, and the brief-retrieval-store endpoint.

All reads go through ``foreign_reader_session_factory`` (story 02 /
ALP-666) on ``request.app.state.foreign_reader_session_factory``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.expression import ColumnElement

from alphamind.state.tables.activity_log import ActivityLogRow
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
# 05d: Per-invocation detail — Pydantic models
# ---------------------------------------------------------------------------


class ActivityLogEntry(BaseModel):
    """One activity-log row relevant to a single invocation."""

    entry_id: str
    entry_at: str
    event_type: str
    event_group: str
    position_id: str | None
    order_id: str | None
    thesis_id: str | None
    source: str
    detail_json: str


class ArchiveSection(BaseModel):
    """Metadata about one directory section inside an invocation archive."""

    section: str
    """Section subdirectory name (e.g. ``distillation``, ``analysis``)."""
    files: list[str]
    """Filenames present in the section; empty when the section is absent."""


class InvocationDetailHeader(BaseModel):
    """Header pane: top-level metadata for one invocation."""

    invocation_id: str
    run_type: str
    started_at: str
    ended_at: str | None
    status: str
    phase1_completed_at: str | None
    phase2_completed_at: str | None
    duration_seconds: float | None
    trigger_type: str
    trigger_reason: str
    git_sha: str
    active_profile: str
    active_regime: str
    active_mode: str
    abort_reason: str | None
    error_summary: str | None


class InvocationDetailResponse(BaseModel):
    """Full per-invocation detail response (ALP-674).

    ``archive_root`` is the resolved invocation archive directory path.
    ``archive_sections`` lists the sub-sections present on disk.
    ``pm_entries`` are ``PM_DECISION`` activity-log rows.
    ``command_fill_entries`` are order-submission / fill rows.
    """

    header: InvocationDetailHeader
    archive_root: str | None = Field(
        default=None,
        description="Resolved archive directory for this invocation; null when absent.",
    )
    archive_sections: list[ArchiveSection] = Field(default_factory=list)
    pm_entries: list[ActivityLogEntry] = Field(default_factory=list)
    command_fill_entries: list[ActivityLogEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 05d: Per-invocation detail — archive path resolution
# ---------------------------------------------------------------------------

# Event-type sets used for activity-log pane filtering.
_PM_EVENT_TYPES = frozenset({"PM_DECISION", "ENVELOPE_REJECTED", "ENVELOPE_PARSE_FAILED"})
_COMMAND_FILL_EVENT_TYPES = frozenset(
    {
        "ORDER_SUBMITTED",
        "ORDER_FILLED",
        "ORDER_PARTIALLY_FILLED",
        "ORDER_CANCELLED",
        "ORDER_EXPIRED",
        "ORDER_REJECTED",
        "ORDER_MODIFIED",
        "COMMAND_ABANDONED",
        "GUARDRAIL_REJECTION",
    }
)

# Recognised archive sub-sections (order matches the design-doc pane table).
_ARCHIVE_SECTIONS = ("distillation", "analysis", "decision", "execution")


def _archive_base_dir() -> Path:
    """Return ``%USERPROFILE%/AlphaMind/archive`` (cross-platform)."""
    profile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    return Path(profile) / "AlphaMind" / "archive"


def _resolve_invocation_archive(start_at: str) -> Path | None:
    """Return the archive directory for the given ``start_at`` timestamp.

    Archive layout (from infrastructure.md § Layer 2)::

        %USERPROFILE%/AlphaMind/archive/<date>/<HH-MM-SS>_<run_type>/

    The ``start_at`` field encodes the timestamp; the run-type segment is
    embedded in the directory name but not queryable from the DB alone.
    We resolve by scanning the date bucket for a directory whose name
    starts with the expected time prefix (``HH-MM-SS``).
    """
    try:
        ts = datetime.fromisoformat(start_at.rstrip("Z"))
    except (ValueError, TypeError):
        return None

    date_str = ts.strftime("%Y-%m-%d")
    time_prefix = ts.strftime("%H-%M-%S")
    date_dir = _archive_base_dir() / date_str
    if not date_dir.is_dir():
        return None

    for entry in date_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(time_prefix):
            return entry
    return None


def _scan_archive_sections(archive_root: Path) -> list[ArchiveSection]:
    """Return metadata about known sub-sections present in *archive_root*."""
    sections: list[ArchiveSection] = []
    for section_name in _ARCHIVE_SECTIONS:
        section_dir = archive_root / section_name
        if section_dir.is_dir():
            files = sorted(f.name for f in section_dir.iterdir() if f.is_file())
        else:
            files = []
        sections.append(ArchiveSection(section=section_name, files=files))
    return sections


# ---------------------------------------------------------------------------
# 05d: Per-invocation detail — DB helpers
# ---------------------------------------------------------------------------


def _activity_row_to_entry(row: ActivityLogRow) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=row.entry_id,
        entry_at=row.entry_at,
        event_type=row.event_type,
        event_group=row.event_group,
        position_id=row.position_id,
        order_id=row.order_id,
        thesis_id=row.thesis_id,
        source=row.source,
        detail_json=row.detail_json,
    )


async def _invocation_detail_fetch(
    session: AsyncSession,
    invocation_id: str,
) -> InvocationDetailResponse | None:
    """Fetch full detail for one invocation; return ``None`` if not found."""
    inv_result = await session.execute(
        select(InvocationRow).where(InvocationRow.invocation_id == invocation_id)
    )
    row: InvocationRow | None = inv_result.scalars().first()
    if row is None:
        return None

    # Derive header fields.
    status = _derive_status(row.phase1_completed_at, row.phase2_completed_at)
    ended_at = row.phase2_completed_at or row.phase1_completed_at
    duration = _compute_duration(row.start_at, ended_at) if ended_at else None
    abort_reason = _extract_abort_reason(row.snapshot_metadata_json)
    error_summary: str | None = None
    if row.snapshot_metadata_json:
        try:
            meta = json.loads(row.snapshot_metadata_json)
            if isinstance(meta, dict):
                error_summary = meta.get("error_summary")
        except (ValueError, TypeError):
            pass

    header = InvocationDetailHeader(
        invocation_id=row.invocation_id,
        run_type=row.trigger_source,
        started_at=row.start_at,
        ended_at=ended_at,
        status=status,
        phase1_completed_at=row.phase1_completed_at,
        phase2_completed_at=row.phase2_completed_at,
        duration_seconds=duration,
        trigger_type=row.trigger_type,
        trigger_reason=row.trigger_reason,
        git_sha=row.git_sha_at_invocation,
        active_profile=row.active_profile,
        active_regime=row.active_regime,
        active_mode=row.active_mode,
        abort_reason=abort_reason,
        error_summary=error_summary,
    )

    # Activity-log panes: PM_DECISION and command/fill entries.
    al_result = await session.execute(
        select(ActivityLogRow)
        .where(ActivityLogRow.invocation_id == invocation_id)
        .order_by(ActivityLogRow.entry_at)
    )
    all_entries: Sequence[ActivityLogRow] = al_result.scalars().all()
    pm_entries = [_activity_row_to_entry(e) for e in all_entries if e.event_type in _PM_EVENT_TYPES]
    command_fill_entries = [
        _activity_row_to_entry(e) for e in all_entries if e.event_type in _COMMAND_FILL_EVENT_TYPES
    ]

    # Archive resolution.
    archive_root = _resolve_invocation_archive(row.start_at)
    archive_root_str = str(archive_root) if archive_root else None
    archive_sections = _scan_archive_sections(archive_root) if archive_root else []

    return InvocationDetailResponse(
        header=header,
        archive_root=archive_root_str,
        archive_sections=archive_sections,
        pm_entries=pm_entries,
        command_fill_entries=command_fill_entries,
    )


# ---------------------------------------------------------------------------
# 05d: Brief-retrieval store — Pydantic models + helpers
# ---------------------------------------------------------------------------

# Reference-ID prefix vocabulary from synthesizer.md.
_VALID_REF_PREFIXES = frozenset({"SA-TECH", "SA-FIN", "SA-ENERGY", "CR", "QR", "AR"})


class BriefSection(BaseModel):
    """One indexed section from the brief-retrieval store."""

    ref_id: str
    """Full reference ID, e.g. ``SA-TECH-3``."""
    content: str
    """Markdown content of this brief section."""


class BriefRetrievalResponse(BaseModel):
    """Response envelope for ``GET /api/views/brief-retrieval``."""

    invocation_id: str
    ref_prefix: str
    sections: list[BriefSection]


def _prefix_to_filenames(ref_prefix: str) -> list[str]:
    """Return the expected brief filenames for a given reference-ID prefix.

    Mapping follows infrastructure.md § Layer 2 archive layout and
    synthesizer.md reference-ID taxonomy.
    """
    mapping: dict[str, list[str]] = {
        "SA-TECH": ["tech_semis_brief.md"],
        "SA-FIN": ["financials_brief.md"],
        "SA-ENERGY": ["energy_brief.md"],
        "QR": ["qualitative_brief.md"],
        "AR": ["adaptive_research.md"],
        "CR": ["synthesis.md"],
    }
    return mapping.get(ref_prefix, [])


def _extract_brief_sections(content: str, ref_prefix: str) -> list[BriefSection]:
    """Split a brief markdown file into per-reference-ID sections.

    Sections are delimited by lines of the form ``[PREFIX-N]`` or
    ``## PREFIX-N`` or ``**[PREFIX-N]**``. Each matched block is one
    ``BriefSection``. If no structured markers are found, the entire
    file is returned as a single section with ref_id ``{prefix}-1``.
    """
    import re

    pattern = re.compile(
        rf"(?:^##\s+|\[|\*\*\[)({re.escape(ref_prefix)}-\d+)(?:\]|\*\*)?",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(content))
    if not matches:
        # No structured markers — wrap the entire file.
        return [BriefSection(ref_id=f"{ref_prefix}-1", content=content.strip())]

    sections: list[BriefSection] = []
    for i, match in enumerate(matches):
        ref_id = match.group(1)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[start:end].strip()
        sections.append(BriefSection(ref_id=ref_id, content=block))
    return sections


async def _brief_retrieval_fetch(
    session: AsyncSession,
    invocation_id: str,
    ref_prefix: str,
) -> BriefRetrievalResponse | None:
    """Return brief sections for *invocation_id* keyed by *ref_prefix*.

    Returns ``None`` if the invocation does not exist.
    """
    inv_result = await session.execute(
        select(InvocationRow).where(InvocationRow.invocation_id == invocation_id)
    )
    row: InvocationRow | None = inv_result.scalars().first()
    if row is None:
        return None

    archive_root = _resolve_invocation_archive(row.start_at)
    sections: list[BriefSection] = []
    if archive_root:
        filenames = _prefix_to_filenames(ref_prefix)
        for filename in filenames:
            candidate = archive_root / "analysis" / filename
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
                sections.extend(_extract_brief_sections(text, ref_prefix))

    return BriefRetrievalResponse(
        invocation_id=invocation_id,
        ref_prefix=ref_prefix,
        sections=sections,
    )


# ---------------------------------------------------------------------------
# 05d: Archive-file streaming — path-traversal guard
# ---------------------------------------------------------------------------


def _safe_archive_file(
    archive_root: Path,
    section: str,
    filename: str,
) -> Path:
    """Resolve and validate the archive file path.

    Raises :class:`ValueError` if the resolved path does not sit under
    *archive_root* (path-traversal protection).
    """
    # Resolve both the root and the candidate to eliminate ``..`` segments.
    resolved_root = archive_root.resolve()
    candidate = (archive_root / section / filename).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Path traversal detected: {candidate!r} escapes {resolved_root!r}")
    return candidate


async def _stream_file(path: Path) -> AsyncIterator[bytes]:
    """Yield the file contents in 64 KiB chunks."""
    chunk_size = 65536
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_history_router() -> APIRouter:  # noqa: C901 — router factory; each branch is a distinct HTTP endpoint
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

    # -----------------------------------------------------------------------
    # Story 05d (ALP-674): per-invocation detail + archive streaming +
    # brief-retrieval store endpoints.
    # -----------------------------------------------------------------------

    @router.get(
        "/runs/{invocation_id}",
        response_model=InvocationDetailResponse,
        summary="Full detail for one invocation (header + pane data).",
    )
    async def invocation_detail_get(
        invocation_id: str,
        reader: Annotated[async_sessionmaker[AsyncSession], Depends(_reader_factory)] = ...,  # type: ignore[assignment]
    ) -> InvocationDetailResponse:
        """Return the full detail payload for *invocation_id*.

        Includes the header pane (run metadata), archive section listing,
        PM-decision activity-log entries, and command/fill activity-log
        entries.  Returns 404 when *invocation_id* is not found.
        """
        async with reader() as session:
            result = await _invocation_detail_fetch(session, invocation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Invocation not found.")
        return result

    @router.get(
        "/runs/{invocation_id}/archive/{section}/{filename}",
        summary="Stream a raw archive file for one invocation.",
    )
    async def invocation_detail_archive_file(
        invocation_id: str,
        section: str,
        filename: str,
        reader: Annotated[async_sessionmaker[AsyncSession], Depends(_reader_factory)] = ...,  # type: ignore[assignment]
    ) -> StreamingResponse:
        """Stream the raw archive file at ``{section}/{filename}``.

        Path-traversal protection: the resolved path must sit under the
        invocation's archive root (``..`` sequences are rejected with
        400).  Returns 404 if the invocation or file is not found.
        """
        async with reader() as session:
            inv_result = await session.execute(
                select(InvocationRow).where(InvocationRow.invocation_id == invocation_id)
            )
            row: InvocationRow | None = inv_result.scalars().first()

        if row is None:
            raise HTTPException(status_code=404, detail="Invocation not found.")

        archive_root = _resolve_invocation_archive(row.start_at)
        if archive_root is None:
            raise HTTPException(status_code=404, detail="Archive directory not found.")

        try:
            file_path = _safe_archive_file(archive_root, section, filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="Archive file not found.")

        media_type = "application/json" if filename.endswith(".json") else "text/markdown"
        return StreamingResponse(
            _stream_file(file_path),
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    @router.get(
        "/brief-retrieval",
        response_model=BriefRetrievalResponse,
        summary="Brief-retrieval store sections keyed by reference-ID prefix.",
    )
    async def brief_retrieval_get(
        invocation_id: Annotated[str, Query()],
        ref_prefix: Annotated[str, Query()],
        reader: Annotated[async_sessionmaker[AsyncSession], Depends(_reader_factory)] = ...,  # type: ignore[assignment]
    ) -> BriefRetrievalResponse:
        """Return brief sections for *invocation_id* matching *ref_prefix*.

        *ref_prefix* must be one of the recognised synthesizer reference
        prefixes (``SA-TECH``, ``SA-FIN``, ``SA-ENERGY``, ``CR``, ``QR``,
        ``AR``).  Returns 400 for unrecognised prefixes and 404 when the
        invocation is not found.
        """
        if ref_prefix not in _VALID_REF_PREFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown ref_prefix {ref_prefix!r}. "
                f"Valid prefixes: {sorted(_VALID_REF_PREFIXES)}",
            )
        async with reader() as session:
            result = await _brief_retrieval_fetch(session, invocation_id, ref_prefix)
        if result is None:
            raise HTTPException(status_code=404, detail="Invocation not found.")
        return result

    return router
