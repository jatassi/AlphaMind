"""Shared envelope types and ISO helpers for on-demand tool outputs — ALP-247.

Every tool output carries ``data_freshness`` and ``quality`` fields.
The ``ToolQuality`` enum names the valid quality states across all tools.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel


class ToolQuality(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    PARTIAL_NO_TRANSCRIPT = "partial_no_transcript"  # earnings_commentary-specific


class ToolEnvelope(BaseModel, frozen=True):
    """Mixin shape for every tool output: every payload carries data_freshness + quality."""

    data_freshness: datetime
    quality: ToolQuality


def parse_iso(raw: str) -> datetime:
    """Parse an ISO 8601 timestamp, treating Z-suffix and naive values as UTC."""
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def format_iso(dt: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 string with Z suffix."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
