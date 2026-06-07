"""Repository Protocols for the counterfactual replay engine (ALP-558).

Three read-only repository protocols consumed by :func:`check_eligibility`
(story 04) and extended by later stories (05a widens :class:`BarRepository`,
05c widens :class:`OptionsSnapshotRepository`).

SQL-backed implementations are wired at the composition root (story 08).
In-memory fakes are used in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from alphamind.execution.counterfactual_replay_engine.iv_lookup import IVSnapshotLookupResult

__all__ = [
    "BarRepository",
    "CorporateActionRepository",
    "OhlcvBar",
    "OptionsSnapshotRepository",
]


@dataclass(frozen=True, slots=True)
class OhlcvBar:
    """One underlying OHLCV bar the equity / option simulators walk.

    Mirrors the subset of the ``ohlcv_bars`` columns the trigger walk consumes.
    The OHLC fields carry the **unadjusted** prices: the replay window is short
    enough that any corporate action in it is eligibility-excluded
    (``CORPORATE_ACTION_IN_WINDOW``), so unadjusted prices are the prices a fill
    would actually have crossed. The SQL-backed implementation (story 08) reads
    the ``unadj_*`` columns into ``open`` / ``high`` / ``low`` / ``close``.

    Both ``period_start`` and ``period_end`` are tz-aware UTC; the simulator
    compares bar timestamps against the proposal's tz-aware deadlines.
    """

    period_start: datetime
    period_end: datetime
    open: float
    high: float
    low: float
    close: float
    adj_volume: int


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

    def load_bars(
        self,
        *,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> tuple[OhlcvBar, ...]:
        """Return bars over [*start*, *end*] ordered ascending by ``period_start``.

        Uses the finest available timeframe in the underlying ``ohlcv_bars``
        table (per parent decision (D), 15-minute in production). The simulator
        (stories 05a, 06, 07) walks the returned sequence; an empty tuple means
        no bars in range (eligibility's ``has_bars_over_window`` gate has
        already excluded that case for EVALUATED proposals).
        """
        ...


class OptionsSnapshotRepository(Protocol):
    """Read access to per-contract implied-volatility snapshots.

    Widened in story 05c (ALP-561) to add :meth:`resolve_contract_ticker` and
    :meth:`lookup_iv`. The SQL-backed implementation is
    :class:`~alphamind.execution.counterfactual_replay_engine.iv_lookup.SqlOptionsSnapshotRepository`;
    in-memory fakes only need to implement the methods they exercise.
    """

    def has_snapshot_at_or_before(
        self,
        contract_ticker: str,
        when: datetime,
    ) -> bool:
        """Return ``True`` if a non-null IV snapshot exists at or before *when*.

        *contract_ticker* is the Polygon OCC symbol
        (``O:{UNDERLYING}{YYMMDD}{C|P}{round(strike*1000):08d}``) that
        ``options_contract_snapshots`` is keyed by — see ``occ_symbol_for_options``.
        Only non-null IV rows are counted so the eligibility pre-check (story 04)
        agrees with :meth:`lookup_iv`. Absence triggers ``DATA_MISSING``.
        """
        ...

    def resolve_contract_ticker(
        self,
        *,
        underlying: str,
        strike: Decimal,
        expiration: date,
        contract_type: Literal["call", "put"],
    ) -> str:
        """Build the canonical Polygon OCC ``contract_ticker`` for an option.

        Pure: delegates to
        :func:`~alphamind.execution.counterfactual_replay_engine.iv_lookup.resolve_contract_ticker`.
        The engine driver uses this to derive the contract key from proposal
        fields without float-equality on ``strike_price``.
        """
        ...

    def lookup_iv(
        self,
        *,
        contract_ticker: str,
        target_ts: datetime,
    ) -> IVSnapshotLookupResult | None:
        """Return the nearest usable IV snapshot at or before *target_ts*.

        Returns ``None`` when no snapshot exists at-or-before the target, or
        when all at-or-before snapshots have ``implied_volatility IS NULL``
        (the engine driver maps both to ``DATA_MISSING``). On a hit, the
        returned :class:`~.iv_lookup.IVSnapshotLookupResult` carries
        ``implied_volatility`` (always non-None), ``underlying_price_at_snapshot``
        (nullable), ``snapshot_ts``, and ``lag_minutes`` for the confidence
        classifier (story 05b).
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
