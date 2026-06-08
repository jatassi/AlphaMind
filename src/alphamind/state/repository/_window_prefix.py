"""Second-precision timestamp-prefix helper for window-range reads (ALP-912 / 06g).

Every ISO-8601 timestamp the codebase writes shares a ``YYYY-MM-DDTHH:MM:SS``
second-precision prefix, but the sub-second *suffix* differs by writer: the
scheduler / operator paths emit second precision
(``strftime("%Y-%m-%dT%H:%M:%SZ")``) while recovery paths emit microsecond
precision (``isoformat().replace("+00:00", "Z")``). The suffixes therefore differ
(``Z`` 0x5A vs ``.`` 0x2E vs ``+`` 0x2B), so a naive lexicographic range filter
over a raw Text timestamp column mis-sorts at sub-second boundaries.

Comparing a window bound's second prefix against the column's second prefix
(``substr(<column>, 1, SECOND_PREFIX_LEN)``) is chronologically faithful at second
granularity for *every* stored value regardless of which writer produced it — the
resolution callers need for invocation-start and thesis-resolution windows. The
column-specific rationale (``invocations.start_at`` vs ``theses.resolution_timestamp``)
stays as an inline note at each call site.
"""

from __future__ import annotations

from datetime import UTC, datetime

#: Length of the ``YYYY-MM-DDTHH:MM:SS`` second-precision prefix shared by every
#: ISO-8601 timestamp the codebase writes, regardless of its sub-second suffix.
SECOND_PREFIX_LEN = 19


def second_prefix(value: datetime) -> str:
    """Render a window bound as its ``YYYY-MM-DDTHH:MM:SS`` second prefix (UTC).

    The bound is normalized to UTC and truncated to second precision, so it
    compares faithfully against ``substr(<text-timestamp-column>, 1,
    SECOND_PREFIX_LEN)`` for any stored value regardless of its sub-second suffix.
    """
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


__all__ = ["SECOND_PREFIX_LEN", "second_prefix"]
