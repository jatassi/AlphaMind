"""Repository Protocols for the counterfactual replay engine (ALP-558).

Three read-only repository protocols consumed by :func:`check_eligibility`
(story 04) and extended by later stories (05a widens :class:`BarRepository`,
05c widens :class:`OptionsSnapshotRepository`).

SQL-backed implementations are wired at the composition root (story 08).
In-memory fakes are used in tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

__all__ = [
    "BarRepository",
    "CorporateActionRepository",
    "OptionsSnapshotRepository",
]


class BarRepository(Protocol):
    """Read access to the underlying OHLCV bar stream."""

    def has_bars_over_window(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        """Return ``True`` if bar data is available for *ticker* over [*start*, *end*].

        Used by :func:`check_eligibility` to gate ``DATA_MISSING``; a ``False``
        result causes the replay to be recorded as unevaluable.
        """
        ...


class OptionsSnapshotRepository(Protocol):
    """Read access to per-contract implied-volatility snapshots."""

    def has_snapshot_at_or_before(
        self,
        contract_ticker: str,
        when: datetime,
    ) -> bool:
        """Return ``True`` if a non-null IV snapshot exists at or before *when*.

        Used by :func:`check_eligibility` for option proposals; absence triggers
        ``DATA_MISSING``.
        """
        ...


class CorporateActionRepository(Protocol):
    """Read access to the corporate-actions record stream."""

    def has_action_in_window(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        """Return ``True`` if any corporate action falls within [*start*, *end*].

        Used by :func:`check_eligibility` to gate ``CORPORATE_ACTION_IN_WINDOW``.
        """
        ...
