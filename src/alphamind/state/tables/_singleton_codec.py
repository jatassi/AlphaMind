"""Helpers shared by the table codecs.

Covers the singleton tables (``cash_ledger`` + ``drawdown_state``) and
the feedback-loop tables (``validations``, ``validation_outcomes``,
``retrospective_reports``, ``retrospective_decisions``, ``theses``).

All timestamps are stored as ISO 8601 text with a UTC ``Z`` suffix.
``datetime_to_iso_z`` is the write-side serializer; ``iso_z_to_datetime``
is its symmetric inverse on the read side.
"""

from __future__ import annotations

from datetime import datetime


def datetime_to_iso_z(value: datetime, *, field_name: str) -> str:
    """Render a tz-aware UTC datetime as ISO 8601 with ``Z`` suffix.

    Raises ``ValueError`` when ``value`` is naive — the codecs reject
    naive inputs upfront so the storage layer never persists ambiguous
    timestamps.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field_name} must be timezone-aware; got {value!r}"
        raise ValueError(msg)
    return value.isoformat().replace("+00:00", "Z")


def iso_z_to_datetime(text: str) -> datetime:
    """Parse an ISO 8601 ``Z``-suffix string back to a ``datetime``.

    This is the exact inverse of :func:`datetime_to_iso_z`.  On
    Python 3.11+ ``datetime.fromisoformat`` accepts the ``Z`` suffix
    written by the write side, so the round-trip is exact.
    """
    return datetime.fromisoformat(text)
