"""Empty-window diagnostic for the news pipeline.

The qualitative-research news digest, the ``news_search`` on-demand tool, and
each domain researcher's per-sector input bundle all read from
``news_articles`` over a time window. When that read returns zero rows, the
LLM agents downstream cannot distinguish three structurally different causes
unless the harness tells them which one applies:

1. ``NO_ROWS_IN_DB`` — the ``news_articles`` table is empty (bootstrap state
   or post-reset, no collector ingestion has landed).
2. ``COLLECTOR_INACTIVE`` — rows exist, but the most recent ingestion finished
   before the caller's window opened. The collector is paused (off-hours
   schedule) or failing (vendor API outage, authentication error).
3. ``NO_HEADLINES_IN_WINDOW`` — the collector is current (latest ingestion is
   inside the window) but no headlines landed in the queried sub-range; the
   window is legitimately empty.

Consumers translate the classification into their own surface (a header
reason line, a tool-envelope reason code, etc.). Call :func:`diagnose_empty_news`
only when the caller's own window query returned no rows; the diagnostic
adds one ``MAX(ingested_at)`` round-trip and classifies in constant time.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.persistence.models import NewsArticles

# NOTE: ``format_iso`` / ``parse_iso`` live in ``alphamind.analysis.tools._envelope``.
# Importing that submodule runs the ``tools`` package ``__init__``, which eagerly
# builds the on-demand-tool registry — and one of those tools (``news_search``)
# imports ``NewsEmptyReason`` back from this module. A module-level import here
# therefore forms a circular import whenever ``news_freshness`` is the first
# module in the chain to touch the ``tools`` package. Both helpers are used only
# inside the functions below, so the import is deferred to call time, by which
# point this module is fully initialized.

__all__ = [
    "NewsEmptyDiagnosis",
    "NewsEmptyReason",
    "diagnose_empty_news",
    "render_empty_reason_text",
]


class NewsEmptyReason(StrEnum):
    """Classification of why a ``news_articles`` window read returned no rows."""

    NO_ROWS_IN_DB = "no_rows_in_db"
    COLLECTOR_INACTIVE = "collector_inactive"
    NO_HEADLINES_IN_WINDOW = "no_headlines_in_window"


class NewsEmptyDiagnosis(NamedTuple):
    """Reason classification plus the latest ``ingested_at`` used to derive it.

    Invariant maintained by :func:`diagnose_empty_news`: ``latest_ingested_at``
    is ``None`` iff ``reason`` is ``NO_ROWS_IN_DB``. External callers that
    construct ``NewsEmptyDiagnosis`` directly must honor the same invariant
    or :func:`render_empty_reason_text` raises.
    """

    reason: NewsEmptyReason
    latest_ingested_at: datetime | None


def diagnose_empty_news(
    session: Session,
    *,
    window_start: datetime,
) -> NewsEmptyDiagnosis:
    """Classify why no ``news_articles`` rows fell in the caller's window.

    ``window_start`` is the lower bound the caller filtered on
    (``last_invocation_time`` for the digest, ``as_of - lookback_hours`` for
    the ``news_search`` tool). The classification reflects collector state
    relative to that bound, not relative to "now".
    """
    from alphamind.analysis.tools._envelope import parse_iso  # deferred — see module note

    raw = session.execute(select(func.max(NewsArticles.ingested_at))).scalar()
    if raw is None:
        return NewsEmptyDiagnosis(reason=NewsEmptyReason.NO_ROWS_IN_DB, latest_ingested_at=None)
    latest = parse_iso(raw)
    if latest < window_start:
        return NewsEmptyDiagnosis(
            reason=NewsEmptyReason.COLLECTOR_INACTIVE, latest_ingested_at=latest
        )
    return NewsEmptyDiagnosis(
        reason=NewsEmptyReason.NO_HEADLINES_IN_WINDOW, latest_ingested_at=latest
    )


def render_empty_reason_text(diagnosis: NewsEmptyDiagnosis) -> str:
    """Render a one-line, LLM-readable explanation of the empty-window cause."""
    from alphamind.analysis.tools._envelope import format_iso  # deferred — see module note

    if diagnosis.reason is NewsEmptyReason.NO_ROWS_IN_DB:
        return (
            "news_articles table is empty — no collector ingestion has "
            "landed against this database (bootstrap state or post-reset)."
        )
    latest = diagnosis.latest_ingested_at
    if latest is None:
        raise ValueError(
            f"NewsEmptyDiagnosis with reason={diagnosis.reason.value} "
            "requires latest_ingested_at; got None"
        )
    latest_iso = format_iso(latest)
    if diagnosis.reason is NewsEmptyReason.COLLECTOR_INACTIVE:
        return (
            f"collector inactive — latest ingestion {latest_iso} precedes "
            "this window, indicating a paused schedule (off-hours) or a "
            "vendor API failure."
        )
    return (
        f"collector current (latest ingestion {latest_iso}) but no headlines landed in this window."
    )
