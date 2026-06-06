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

        *ticker* is the underlying equity symbol for analyst and
        ``PositionAssessment`` proposals. A ``PendingOrderAssessment`` carries no
        underlying, so :func:`check_eligibility` passes its ``position_id``
        instead; the SQL-backed implementation (story 08) must resolve that to
        the underlying ticker before querying ``ohlcv_bars``. Used to gate
        ``DATA_MISSING``; a ``False`` result records the replay as unevaluable.
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

        *contract_ticker* is the Polygon OCC symbol
        (``O:{UNDERLYING}{YYMMDD}{C|P}{round(strike*1000):08d}``) that
        ``options_contract_snapshots`` is keyed by — see ``occ_symbol_for_options``.
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

        *ticker* follows the same convention as
        :meth:`BarRepository.has_bars_over_window` — the underlying symbol, or a
        ``PendingOrderAssessment``'s ``position_id`` the story-08 implementation
        resolves. Used by :func:`check_eligibility` to gate
        ``CORPORATE_ACTION_IN_WINDOW``.
        """
        ...
