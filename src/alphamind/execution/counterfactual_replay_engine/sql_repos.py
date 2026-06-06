"""SQL-backed bar / corporate-action repositories for the replay engine (ALP-564).

Synchronous SQLAlchemy ``Session`` implementations of the
:class:`~.repos.BarRepository` and :class:`~.repos.CorporateActionRepository`
Protocols (defined in story 04). The options-snapshot Protocol implementation
lives in :mod:`.iv_lookup` (story 05c) and is reused unchanged.

Both repositories accept the *underlying equity ticker* for analyst and
``PositionAssessment`` proposals, and the *position_id* for
``PendingOrderAssessment`` proposals (which carry no underlying — see
:meth:`~.repos.BarRepository.has_bars_over_window`). The ``position_id`` form is
resolved to the underlying via the ``positions`` row before querying
``ohlcv_bars`` / ``corporate_actions``; without that resolution every
pending-order cancel/modify replay would falsely read ``DATA_MISSING``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar
from alphamind.persistence.models import CorporateActions, OhlcvBars
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record

__all__ = ["SqlBarRepository", "SqlCorporateActionRepository"]

_TIMEFRAME = "15min"
"""The finest production timeframe (parent decision (D)); the simulators walk it."""

_BAR_INTERVAL = timedelta(minutes=15)
"""The 15-minute bar period; ``start`` is floored to this boundary to find its bar."""


def _floor_to_bar_start(moment: datetime) -> datetime:
    """Floor *moment* to the 15-minute bar boundary that contains it.

    The proposal-containing bar's ``period_start`` is the largest 15-minute
    boundary ``<= moment``; flooring *moment* to it lets the load query select
    that bar by ``period_start`` directly, independent of how ``period_end`` is
    stored (the production polygon collector writes ``period_end == period_start``,
    so an overlap predicate on ``period_end`` would drop the containing bar).
    """
    discard = timedelta(
        minutes=moment.minute % 15,
        seconds=moment.second,
        microseconds=moment.microsecond,
    )
    return moment - discard


def _parse_ts(raw: str) -> datetime:
    """Parse a stored ISO-8601 bar / action timestamp into a tz-aware UTC datetime.

    The collector writes UTC ISO strings; tz-naive hand-crafted test rows are
    normalised by attaching UTC so the simulator's tz-aware comparisons hold.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _resolve_underlying(session: Session, ticker_or_position_id: str) -> str:
    """Resolve a repo key to the underlying equity ticker.

    Analyst / ``PositionAssessment`` proposals pass the underlying directly, so
    when no ``positions`` row matches we return the key unchanged (it is already
    a ticker). A ``PendingOrderAssessment`` passes its ``position_id``; that row
    exists, and its ``details`` payload carries the underlying — equity
    ``ticker`` or option ``underlying_ticker``.
    """
    row = session.execute(
        select(PositionRow).where(PositionRow.position_id == ticker_or_position_id)
    ).scalar_one_or_none()
    if row is None:
        return ticker_or_position_id
    details = row_to_record(row).details
    if isinstance(details, EquityPositionDetails):
        return str(details.ticker)
    if isinstance(details, OptionsPositionDetails):
        return str(details.underlying_ticker)
    # Multi-leg strategy positions are eligibility-excluded upstream; if one
    # reaches here, fall back to the raw key rather than guessing a leg.
    return ticker_or_position_id


class SqlBarRepository:
    """``Session``-backed :class:`~.repos.BarRepository` over ``ohlcv_bars``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def has_bars_over_window(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        """Return ``True`` if the proposal-containing bar exists for [*start*, *end*].

        Mirrors :meth:`load_bars`: the earliest bar at or after the 15-minute
        floor of *start* must be the bar that *contains* *start*
        (``period_start == floor(start)``). If the containing bar is missing
        while later bars exist, ``load_bars`` would return a sequence whose
        ``bars[0]`` is no longer the proposal-containing bar and the entry
        simulators would silently shift one bar late — so a missing containing
        bar is "no data" (the driver records ``DATA_MISSING``). A
        ``period_end``-overlap predicate is not usable: the prod collector writes
        ``period_end == period_start``, which would degenerate to
        ``period_start > start`` and drop the containing bar.

        *ticker* is the underlying for analyst / ``PositionAssessment``
        proposals or a ``position_id`` for ``PendingOrderAssessment`` (resolved
        to the underlying first).
        """
        resolved = _resolve_underlying(self._session, ticker)
        floored_start = _floor_to_bar_start(start)
        stmt = (
            select(OhlcvBars.period_start)
            .where(
                OhlcvBars.ticker == resolved,
                OhlcvBars.timeframe == _TIMEFRAME,
                OhlcvBars.period_start >= floored_start.isoformat(),
                OhlcvBars.period_start <= end.isoformat(),
            )
            .order_by(OhlcvBars.period_start.asc())
            .limit(1)
        )
        earliest = self._session.execute(stmt).scalar()
        return earliest is not None and earliest == floored_start.isoformat()

    def load_bars(
        self,
        *,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> tuple[OhlcvBar, ...]:
        """Return the 15-minute bars over [*start*, *end*] ascending by ``period_start``.

        The first returned bar is the one whose period *contains* *start*
        (``period_start <= start < period_start + 15min``), not the first bar
        strictly after *start*. The equity market-order entry simulator (story
        05a) fills at ``bars[1].open`` and documents ``bars[0]`` as the
        proposal-containing bar (``window_start`` = the proposal timestamp);
        returning only bars strictly after the proposal would put every market
        entry off by one bar.

        The containing bar is selected by ``period_start`` alone: *start* is
        floored to its 15-minute boundary, and the filter is
        ``floored_start <= period_start <= end``. A ``period_end``-overlap
        predicate cannot be used — the production polygon collector
        (:mod:`alphamind.data_sources.polygon.equity`) writes
        ``period_end == period_start`` for every bar, so ``period_end > start``
        would degenerate to ``period_start > start`` and drop the
        proposal-containing bar in prod.

        The ``unadj_*`` columns map onto the :class:`OhlcvBar` OHLC fields per
        that record's contract (the short replay window is corporate-action-free
        by eligibility, so unadjusted prices are the prices a fill would cross).
        *ticker* may be an underlying or a ``position_id`` (resolved first).
        """
        resolved = _resolve_underlying(self._session, ticker)
        floored_start = _floor_to_bar_start(start)
        stmt = (
            select(OhlcvBars)
            .where(
                OhlcvBars.ticker == resolved,
                OhlcvBars.timeframe == _TIMEFRAME,
                OhlcvBars.period_start >= floored_start.isoformat(),
                OhlcvBars.period_start <= end.isoformat(),
            )
            .order_by(OhlcvBars.period_start.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return tuple(
            OhlcvBar(
                period_start=_parse_ts(row.period_start),
                period_end=_parse_ts(row.period_end),
                open=row.unadj_open,
                high=row.unadj_high,
                low=row.unadj_low,
                close=row.unadj_close,
                adj_volume=row.unadj_volume,
            )
            for row in rows
        )


class SqlCorporateActionRepository:
    """``Session``-backed :class:`~.repos.CorporateActionRepository` over ``corporate_actions``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def has_action_in_window(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        """Return ``True`` if any corporate action's ``ex_date`` falls in [*start*, *end*].

        The window's start / end are tz-aware UTC datetimes; the stored
        ``ex_date`` is a date-only ISO string, so the comparison is on the date
        component (``start.date()`` .. ``end.date()``, inclusive). *ticker*
        follows the dual underlying / ``position_id`` convention.
        """
        resolved = _resolve_underlying(self._session, ticker)
        stmt = (
            select(CorporateActions.action_id)
            .where(
                CorporateActions.ticker == resolved,
                CorporateActions.ex_date >= start.date().isoformat(),
                CorporateActions.ex_date <= end.date().isoformat(),
            )
            .limit(1)
        )
        return self._session.execute(stmt).first() is not None
