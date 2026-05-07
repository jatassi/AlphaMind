"""Helpers shared by the singleton-table codecs (``cash_ledger`` + ``drawdown_state``).

The ``last_updated_at`` column on each singleton row is storage-time
metadata supplied as a kwarg by the write path — it is not carried by
the typed record. Both codecs therefore share the same tz-aware guard
and ISO 8601 ``Z``-suffix serializer.
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
