"""Shared past-date detection helpers for prediction-market questions.

The polymarket vendor leaves questions like ``"Iran closes its airspace by
May 6?"`` listed at their last-traded probability long after May 6 — the
contract only formally resolves later, but consumers should treat the
contract as stale.

Both the distillation ``qual.prediction_market_delta`` block and the
qualitative researcher's input bundle apply this heuristic; centralizing
the logic here keeps them in lockstep. The module is pure (no ORM, no
``Session``) and sits at the distillation package root rather than under
``qualitative/`` so the SQL repository can parse the raw column at the IO
boundary without triggering the qualitative subpackage's eager re-exports.

The QR loader uses this as an exclusion filter (past-dated contracts
disappear from the LLM's input). The distillation compute uses it as a flag
(``is_question_past_dated``) so the synthesizer brief tags the contract for
downstream agents rather than dropping it.
"""

from __future__ import annotations

import re
from datetime import date, datetime

__all__ = [
    "parse_resolution_date",
    "question_references_past_date",
]


_MONTH_NUMBER_BY_NAME: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip

_QUESTION_DATE_PATTERN = re.compile(
    # ``(?!\d)`` after the day suffix prevents the day group from greedily
    # capturing the first two digits of a four-digit year (e.g. ``January
    # 2027`` must NOT match as ``January 20`` with year inferred).
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?!\d)"
    r"(?:[,\s]+(?P<year>\d{4}))?",
    re.IGNORECASE,
)


def _parse_iso_utc_to_date(raw: str) -> date:
    """Parse an ISO 8601 ``Z``-suffixed UTC timestamp to its calendar ``date``."""
    if raw.endswith("Z"):
        return datetime.fromisoformat(raw[:-1]).date()
    return datetime.fromisoformat(raw).date()


def parse_resolution_date(raw: str | None) -> date | None:
    """Parse a contract's ``resolution_date`` text column to a ``date`` (or ``None``)."""
    if not raw:
        return None
    try:
        return _parse_iso_utc_to_date(raw)
    except ValueError:
        return None


def question_references_past_date(
    description: str,
    as_of: datetime,
    *,
    resolution_date: date | None,
) -> bool:
    """True iff the question text references a date before ``as_of`` *within the
    contract's lifetime*.

    Matches month names with optional day suffix (``May 6``, ``May 6th``,
    ``May 3, 2026``). When the year is implicit, it is anchored to the most
    recent occurrence on or before ``resolution_date`` (so ``"May 6"`` on a
    contract resolving ``2026-05-31`` is read as ``2026-05-06``, not
    ``2027-05-06``). Without a ``resolution_date``, the year falls back to
    ``as_of.year``.
    """
    as_of_date = as_of.date()
    for match in _QUESTION_DATE_PATTERN.finditer(description):
        month = _MONTH_NUMBER_BY_NAME[match.group("month").lower()]
        try:
            day = int(match.group("day"))
            year_raw = match.group("year")
            if year_raw is not None:
                referenced = date(int(year_raw), month, day)
            elif resolution_date is not None:
                year = resolution_date.year
                if date(year, month, day) > resolution_date:
                    year -= 1
                referenced = date(year, month, day)
            else:
                referenced = date(as_of_date.year, month, day)
        except ValueError:
            continue
        if referenced < as_of_date:
            return True
    return False
