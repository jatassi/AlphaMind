"""Breadth and market internals computation.

Implements ``compute_breadth_internals``, ``_advance_decline_per_sector``,
and ``_equal_vs_cap_weight``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise

from sqlalchemy.orm import Session

from alphamind.distillation.output import (
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _select_close_series,
    _window_bounds,
)

# Standard EMA windows for market-internals breadth checks. Story 08d names
# 20 / 50 / 200 day EMAs; the trio is a structural convention rather than a
# tunable threshold so the values are encoded as named constants in the
# code rather than as configuration.
EMA_WINDOWS_DAYS: tuple[int, ...] = (20, 50, 200)


def _ema(closes: Sequence[float], window: int) -> float:
    """Exponential moving average over ``closes`` with span ``window``.

    Uses the standard EMA recurrence ``ema_t = alpha * close_t + (1 - alpha) * ema_{t-1}``
    seeded with the simple mean of the first ``window`` closes. When the
    series is shorter than ``window``, falls back to the simple mean of
    available closes (so the EMA value is always defined while the caller
    can flag the calibration state separately).
    """
    if not closes:
        return 0.0
    if len(closes) <= window:
        return float(sum(closes)) / float(len(closes))
    seed = sum(closes[:window]) / window
    alpha = 2.0 / (window + 1.0)
    ema_value = seed
    for close in closes[window:]:
        ema_value = alpha * close + (1.0 - alpha) * ema_value
    return float(ema_value)


def _pct_above_ema(closes_by_ticker: dict[str, list[float]], window: int) -> float:
    """Fraction of tickers whose latest close is above their ``window``-day EMA.

    Returns 0.0 when no ticker has any closes (the empty universe edge
    case); the caller should still emit the block so consumers see the
    calibration tag.
    """
    above = 0
    total = 0
    for closes in closes_by_ticker.values():
        if not closes:
            continue
        total += 1
        ema_value = _ema(closes, window)
        if closes[-1] > ema_value:
            above += 1
    if total == 0:
        return 0.0
    return float(above) / float(total)


def _last_two_closes(closes: list[float]) -> tuple[float, float] | None:
    """Return the final ``(prev, curr)`` close pair, or ``None`` if unavailable."""
    last_pair: tuple[float, float] | None = None
    for prev, curr in pairwise(closes):
        last_pair = (prev, curr)
    return last_pair


def _last_day_return(closes: list[float]) -> float | None:
    """Day-over-day return on the most recent bar, or ``None`` when unavailable."""
    pair = _last_two_closes(closes)
    if pair is None:
        return None
    prev, curr = pair
    if prev <= 0.0:
        return None
    return (curr - prev) / prev


def _advance_decline_per_sector(
    closes_by_ticker: dict[str, list[float]],
    sector_members: dict[str, Sequence[str]],
) -> dict[str, dict[str, int]]:
    """Per-sector ``{advances, declines}`` dict, sorted by sector key."""
    out: dict[str, dict[str, int]] = {}
    for sector in sorted(sector_members):
        advances = 0
        declines = 0
        for ticker in sector_members[sector]:
            ret = _last_day_return(closes_by_ticker.get(ticker, []))
            if ret is None:
                continue
            if ret > 0.0:
                advances += 1
            elif ret < 0.0:
                declines += 1
        out[sector] = {"advances": advances, "declines": declines}
    return out


def _equal_vs_cap_weight(
    closes_by_ticker: dict[str, list[float]],
    universe_tickers: Sequence[str],
    broad_market_closes: list[float],
) -> dict[str, float]:
    """Equal-weight portfolio return vs. broad-market ETF return on the day."""
    universe_returns: list[float] = []
    for ticker in universe_tickers:
        ret = _last_day_return(closes_by_ticker.get(ticker, []))
        if ret is None:
            continue
        universe_returns.append(ret)
    equal_weight_return = sum(universe_returns) / len(universe_returns) if universe_returns else 0.0
    cap_weight_return = _last_day_return(broad_market_closes) or 0.0
    return {
        "equal_weight_return": equal_weight_return,
        "cap_weight_return": cap_weight_return,
        "spread": equal_weight_return - cap_weight_return,
    }


def compute_breadth_internals(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    sectors: Sequence[str],
    sector_members: dict[str, Sequence[str]],
    broad_market_etf: str,
    as_of: datetime,
) -> list[OutputBlock]:
    """Compute the breadth-and-internals block.

    Payload carries:

    - ``pct_above_<n>d_ema`` for each window in :data:`EMA_WINDOWS_DAYS`.
    - ``advance_decline_per_sector`` — ``{sector: {advances, declines}}``.
    - ``equal_vs_cap_weight`` — equal-weight portfolio return vs. the
      ``broad_market_etf`` return on the latest day.

    The block carries both :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`
    and :attr:`OutputAudience.UNIVERSAL_BROADCAST` per the story scope —
    breadth is a universal cross-cutting indicator.
    """
    long_window = max(EMA_WINDOWS_DAYS)
    range_start, range_end = _window_bounds(as_of=as_of, window_days=long_window)

    closes_by_ticker: dict[str, list[float]] = {}
    for ticker in universe_tickers:
        closes_by_ticker[ticker] = _select_close_series(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=range_end,
        )
    broad_market_closes = _select_close_series(
        session,
        ticker=broad_market_etf,
        range_start=range_start,
        range_end=range_end,
    )

    payload: dict[str, object] = {}
    for window in EMA_WINDOWS_DAYS:
        payload[f"pct_above_{window}d_ema"] = _pct_above_ema(closes_by_ticker, window)
    # ``sectors`` is informational; ``sector_members`` drives the actual
    # advance/decline counts and is what the breadth payload exposes.
    _ = sectors
    payload["advance_decline_per_sector"] = _advance_decline_per_sector(
        closes_by_ticker, dict(sector_members)
    )
    payload["equal_vs_cap_weight"] = _equal_vs_cap_weight(
        closes_by_ticker, universe_tickers, broad_market_closes
    )

    n_observed = min(
        (len(closes) for closes in closes_by_ticker.values() if closes),
        default=0,
    )
    state, reason = _calibration_for_window(
        n_observations=n_observed,
        required=long_window,
        input_name="breadth_long_ema_observations",
    )

    block = OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.breadth_internals",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=(),
        regime_context=None,
    )
    return [block]
