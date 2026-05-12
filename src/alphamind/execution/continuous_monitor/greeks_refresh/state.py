"""``LastRefreshState`` — per-position anchor for the greeks-refresh task (story 03a).

The refresh task fires on whichever trigger comes first per parent issue
ALP-123 § Pre-resolved decision (E):

* **Scheduled**: ``now - last_refreshed_at >= greeks_refresh_interval_minutes``.
* **Move-based**: ``|spot - underlying_price_at_last_refresh| /
  underlying_price_at_last_refresh >= greeks_refresh_underlying_move_threshold_pct``.

``LastRefreshState`` stores the two anchors that feed those deltas. The
task seeds it from each open position's existing ``OptionGreeks.as_of_timestamp``
on startup; positions with ``as_of_timestamp is None`` (never refreshed) are
anchored at the Unix epoch so the scheduled delta is unbounded and the task
picks them up on its first inspection tick.

The underlying-price anchor is the cache reading at seed time. If the cache
has not yet observed a quote for the position's underlying (cold start
before the stream task has caught up), the anchor is ``0.0`` and the
move-trigger predicate skips the position until a quote arrives; the
scheduled trigger keeps firing in the meantime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from alphamind.portfolio_state.records.positions import (
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
)

# Sentinel for positions whose greeks have never been refreshed (their
# ``OptionGreeks.as_of_timestamp`` is ``None``). Anchoring at the Unix epoch
# means ``(now - last_refreshed_at)`` is always greater than any positive
# refresh interval, so the scheduled-trigger predicate fires on the first
# inspection tick. tz-aware UTC to match the rest of the codebase.
_EPOCH_ANCHOR: datetime = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class LastRefreshState:
    """Frozen anchor for one position's last greeks refresh.

    ``last_refreshed_at`` is the ``OptionGreeks.as_of_timestamp`` written
    after a successful refresh; ``underlying_price_at_last_refresh`` is the
    cache reading observed at that same instant. The two together drive the
    next refresh decision via the dual-trigger predicate documented in
    ``architecture.md`` § 4d.
    """

    position_id: str
    last_refreshed_at: datetime
    underlying_price_at_last_refresh: float


def _resolve_underlying_ticker(position: PositionRecord) -> str | None:
    """Return the underlying ticker for an option / strategy position.

    Equity positions do not have refreshable greeks; callers filter them
    out before invoking this helper. Returns ``None`` for a strategy with
    no legs (the codec accepts this shape but the position is unreachable
    by the refresh logic).
    """
    details = position.details
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    if isinstance(details, StrategyPositionDetails) and details.legs:
        return details.legs[0].options.underlying_ticker
    return None


def _resolve_last_as_of(position: PositionRecord) -> datetime | None:
    """Extract the existing ``OptionGreeks.as_of_timestamp`` from a position.

    Returns ``None`` for equity positions (no greeks) or for a strategy with
    no legs. The seed loop coerces ``None`` to ``_EPOCH_ANCHOR`` so the
    position is treated as "due now".
    """
    details = position.details
    if isinstance(details, OptionsPositionDetails):
        return details.greeks.as_of_timestamp
    if isinstance(details, StrategyPositionDetails):
        return details.strategy_greeks.as_of_timestamp
    return None


def seed_last_refresh_states(
    positions: Iterable[PositionRecord],
    *,
    underlying_prices: Mapping[str, float],
    now: datetime,
) -> dict[str, LastRefreshState]:
    """Build the initial ``{position_id: LastRefreshState}`` map from open positions.

    ``positions`` should be the option + strategy positions returned by the
    repository's open-positions reader; equity positions are silently
    skipped. ``underlying_prices`` is a snapshot of the
    :class:`UnderlyingPriceCache` at seed time, keyed by ticker. ``now`` is
    used only for module-internal logging contexts — the epoch anchor is
    chosen so it does not flow through.
    """
    del now  # reserved for future invariant-logging; not consumed here.
    states: dict[str, LastRefreshState] = {}
    for position in positions:
        ticker = _resolve_underlying_ticker(position)
        if ticker is None:
            continue
        last_as_of = _resolve_last_as_of(position) or _EPOCH_ANCHOR
        anchor_price = underlying_prices.get(ticker, 0.0)
        states[position.position_id] = LastRefreshState(
            position_id=position.position_id,
            last_refreshed_at=last_as_of,
            underlying_price_at_last_refresh=anchor_price,
        )
    return states
