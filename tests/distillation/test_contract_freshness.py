"""Unit tests for the shared past-date detection helper (ALP-578).

The helper is consumed by both the qualitative researcher's loader (as a
filter) and the distillation compute (as a flag). Tests here exercise the
year-inference branches directly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from alphamind.distillation.contract_freshness import (
    parse_resolution_date,
    question_references_past_date,
)

_AS_OF = datetime(2026, 5, 18, 11, 0, 0, tzinfo=UTC)


class TestQuestionReferencesPastDate:
    def test_resolution_anchored_past(self) -> None:
        """May 6 anchored to a 2026-05-31 resolution is read as 2026-05-06 — past."""
        assert (
            question_references_past_date(
                "Iran closes its airspace by May 6?",
                _AS_OF,
                resolution_date=date(2026, 5, 31),
            )
            is True
        )

    def test_resolution_anchored_future(self) -> None:
        """December 15 anchored to a 2026-12-31 resolution is read as 2026-12-15 — future."""
        assert (
            question_references_past_date(
                "Will the FOMC cut rates by December 15?",
                _AS_OF,
                resolution_date=date(2026, 12, 31),
            )
            is False
        )

    def test_explicit_year_overrides_anchor(self) -> None:
        """An explicit year in the text wins over the resolution anchor."""
        assert (
            question_references_past_date(
                "Will Bitcoin reach $100k by December 31, 2024?",
                _AS_OF,
                resolution_date=date(2026, 12, 31),
            )
            is True
        )

    def test_no_resolution_falls_back_to_as_of_year(self) -> None:
        """Without a resolution_date, the implicit year resolves to as_of.year —
        so ``May 6`` (no year, no resolution) is read as 2026-05-06 (past)."""
        assert (
            question_references_past_date(
                "Event by May 6?",
                _AS_OF,
                resolution_date=None,
            )
            is True
        )

    def test_no_resolution_future_within_as_of_year(self) -> None:
        """``December 15`` (no year, no resolution) is read as 2026-12-15 (future)."""
        assert (
            question_references_past_date(
                "Event by December 15?",
                _AS_OF,
                resolution_date=None,
            )
            is False
        )

    def test_no_date_pattern(self) -> None:
        """Plain text with no date pattern is never past-dated."""
        assert (
            question_references_past_date(
                "Will the Fed cut rates this cycle?",
                _AS_OF,
                resolution_date=date(2026, 12, 31),
            )
            is False
        )

    def test_bare_month_year_not_misread_as_day(self) -> None:
        """``January 2027`` must NOT be matched as ``January 20`` — regression
        for the greedy-day capture that previously flagged forward-looking
        contracts."""
        assert (
            question_references_past_date(
                "Will the S&P close higher in January 2027?",
                _AS_OF,
                resolution_date=date(2027, 12, 31),
            )
            is False
        )

    def test_invalid_day_skipped_silently(self) -> None:
        """``February 31`` is invalid and ignored — neither flagged nor raised."""
        assert (
            question_references_past_date(
                "Event by February 31?",
                _AS_OF,
                resolution_date=date(2026, 12, 31),
            )
            is False
        )


class TestParseResolutionDate:
    def test_z_suffixed_iso_parsed(self) -> None:
        assert parse_resolution_date("2026-05-31T00:00:00Z") == date(2026, 5, 31)

    def test_none_returns_none(self) -> None:
        assert parse_resolution_date(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_resolution_date("") is None

    def test_unparseable_returns_none(self) -> None:
        assert parse_resolution_date("not-a-date") is None
