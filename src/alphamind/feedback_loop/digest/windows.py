"""Week-window helpers shared by the digest read CLI and the snapshot writer (ALP-891).

The pure date helpers (week-Monday, ``[start, end)`` bounds, the trailing-Monday
sequence) and the one async loader shell (:func:`load_week_inputs`) that turns a list
of week-Mondays into the generator's ``(week_label, WindowDataset)`` input. Both the
``digest`` read CLI (``cli.py``) and the ``snapshot`` writer (``snapshot.py``) build the
same trailing-week input, so the construction lives here once rather than duplicated.

The label convention is the week-Monday's ISO date string — the generator treats it
opaquely (it is carried through to the trajectory points and never parsed).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from alphamind.config.models.feedback import FeedbackLoopConfig
    from alphamind.feedback_loop.digest.generator import WeekInput

_DAYS_PER_WEEK = 7


def week_monday(reference: date) -> date:
    """The Monday on or before *reference* — the canonical week start."""
    return reference - timedelta(days=reference.weekday())


def week_bounds(monday: date) -> tuple[datetime, datetime]:
    """The ``[start, end)`` UTC datetimes for the week beginning *monday*."""
    start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    return start, start + timedelta(days=_DAYS_PER_WEEK)


def trailing_weeks(current_monday: date, count: int) -> list[date]:
    """The *count* week-Mondays ending at *current_monday*, oldest-first."""
    return [current_monday - timedelta(weeks=offset) for offset in range(count - 1, -1, -1)]


async def load_week_inputs(
    session: AsyncSession,
    mondays: list[date],
    feedback: FeedbackLoopConfig,
) -> list[WeekInput]:
    """Call ``load_window`` once per week-Monday, building the generator's input."""
    from alphamind.feedback_loop.dataset import load_window

    weeks: list[WeekInput] = []
    for monday in mondays:
        start, end = week_bounds(monday)
        dataset = await load_window(session, start, end, config=feedback)
        weeks.append((monday.isoformat(), dataset))
    return weeks


__all__ = [
    "load_week_inputs",
    "trailing_weeks",
    "week_bounds",
    "week_monday",
]
