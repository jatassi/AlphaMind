"""Tests for :mod:`alphamind._kernel.archive_layout`."""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from pathlib import Path

from alphamind._kernel.archive_layout import (
    find_invocation_archive_dir,
    invocation_archive_dir,
)

# ---------------------------------------------------------------------------
# invocation_archive_dir
# ---------------------------------------------------------------------------

_INVOCATION_ID = "20260525T120000Z-abc123"


def test_invocation_archive_dir_utc(tmp_path: Path) -> None:
    """UTC datetime produces the expected date-partitioned path."""
    as_of = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    result = invocation_archive_dir(
        archive_root=tmp_path,
        as_of=as_of,
        invocation_id=_INVOCATION_ID,
    )
    assert result == tmp_path / "2026-05-25" / _INVOCATION_ID


def test_invocation_archive_dir_non_utc_normalized(tmp_path: Path) -> None:
    """Non-UTC ``as_of`` is normalised to UTC for the date partition.

    A datetime at 2026-05-26 01:00 in UTC+5 is 2026-05-25 20:00 UTC — the
    date partition must reflect the UTC date (2026-05-25), not the local date
    (2026-05-26).
    """
    tz_plus5 = timezone(2 * __import__("datetime").timedelta(hours=5))
    as_of = datetime(2026, 5, 26, 1, 0, 0, tzinfo=tz_plus5)
    result = invocation_archive_dir(
        archive_root=tmp_path,
        as_of=as_of,
        invocation_id=_INVOCATION_ID,
    )
    assert result == tmp_path / "2026-05-25" / _INVOCATION_ID


# ---------------------------------------------------------------------------
# find_invocation_archive_dir
# ---------------------------------------------------------------------------

_SOURCE_INVOCATION_ID = "20260524T090000Z-source"


def test_find_returns_unique_match(tmp_path: Path) -> None:
    """Returns the single matching directory."""
    target = tmp_path / "2026-05-24" / _SOURCE_INVOCATION_ID
    target.mkdir(parents=True)
    result = find_invocation_archive_dir(archive_root=tmp_path, invocation_id=_SOURCE_INVOCATION_ID)
    assert result == target


def test_find_returns_none_on_zero_matches(tmp_path: Path) -> None:
    """Returns ``None`` when no date partition contains the invocation."""
    result = find_invocation_archive_dir(archive_root=tmp_path, invocation_id=_SOURCE_INVOCATION_ID)
    assert result is None


def test_find_returns_none_on_multiple_matches(tmp_path: Path) -> None:
    """Returns ``None`` when the invocation appears under two date partitions (ambiguous)."""
    for date_part in ("2026-05-24", "2026-05-25"):
        (tmp_path / date_part / _SOURCE_INVOCATION_ID).mkdir(parents=True)
    result = find_invocation_archive_dir(archive_root=tmp_path, invocation_id=_SOURCE_INVOCATION_ID)
    assert result is None


def test_find_ignores_non_directory_matches(tmp_path: Path) -> None:
    """A file named after the invocation_id under a date partition is not a match."""
    date_dir = tmp_path / "2026-05-24"
    date_dir.mkdir(parents=True)
    (date_dir / _SOURCE_INVOCATION_ID).write_text("not a dir")
    result = find_invocation_archive_dir(archive_root=tmp_path, invocation_id=_SOURCE_INVOCATION_ID)
    assert result is None
