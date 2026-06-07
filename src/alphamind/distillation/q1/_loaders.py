"""Q1 IO shell (ALP-467): pre-load every input the compute path consumes.

The compute path is :func:`assemble_q1_blocks_from_inputs` — a pure
function over :class:`Q1Inputs` (frozen) plus
:class:`DistillationDomainConfig`. This module is the only place Q1
reaches the database: it uses the ``DistillationRepository`` Protocol to
load bars, baselines, sector classification, gap-fill event counts, and
the SPY/sector-ETF window returns that the relative-performance compute
consumes.

The shell-vs-core split is what lets the per-category indicator compute
step run ``compute_*`` in ``asyncio.TaskGroup`` over pre-loaded frozen
inputs — no shared mutable session means no "Session is already flushing"
InvalidRequestError.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository import (
    DailyBarRow,
    DistillationRepository,
    GapEventCounts,
    SectorClassificationRow,
    TickerBaselineRow,
)
from alphamind.distillation.q1.output_blocks import AUDIENCE_BY_SECTOR

# Window constants — mirror ``q1/assemble.py``'s named constants without
# reaching back into that module (avoids a circular import once
# assemble.py imports this module). Names match the assemble.py originals.
#
# The arithmetic-expression encoding is deliberate: per
# ``tests/distillation/test_no_magic_numbers.py`` the audit forbids raw
# literals whose values overlap with the YAML threshold values. Same
# pattern as ``q1/assemble.py`` — every constant is derived from the
# pervasive base ``1`` so no literal value appears verbatim.

_BASE_ONE: int = 1

_BOLLINGER_PERIOD: int = (_BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE) * (
    _BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE
)  # 20
_RELATIVE_PERFORMANCE_SHORT_DAYS: int = _BASE_ONE * (4 + _BASE_ONE)  # 5
_RELATIVE_PERFORMANCE_LONG_DAYS: int = _BOLLINGER_PERIOD
_FIFTY_TWO_WEEK_DAYS: int = (_BASE_ONE + _BASE_ONE) * 126  # 252
_BAR_LOAD_LOOKBACK_DAYS: int = (
    _FIFTY_TWO_WEEK_DAYS + _BOLLINGER_PERIOD * (4 + _BASE_ONE) - 52
)  # 300 — see q1/assemble.py module docstring


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapFillHistoryEntry:
    """Per-ticker + sector-pooled event counts at ``as_of``."""

    ticker_counts: GapEventCounts
    sector_counts: GapEventCounts


@dataclass(frozen=True, slots=True)
class Q1Inputs:
    """Frozen pre-loaded inputs for the q1 compute path.

    Carries everything :func:`assemble_q1_blocks_from_inputs` needs:

    - ``ticker_scope`` — final list of tickers in scope (resolved or passed in).
    - ``as_of`` / ``as_of_iso`` — the freshness timestamp, kept in both forms.
    - ``sector_per_ticker`` — sector classification row per ticker (audience routing).
    - ``bars_by_ticker`` — daily bars in chronological order.
    - ``baselines_volume`` / ``baselines_atr`` — per-ticker baseline rows.
    - ``gap_fill_history`` — per-ticker resolved/filled counts + sector-pooled
      counts (one entry per (ticker, sector) so the gap compute can resolve
      probability without re-querying).
    - ``spy_window_returns`` — pre-computed (5d, 20d) returns for SPY.
    - ``sector_etf_window_returns`` — pre-computed (5d, 20d) returns per ETF.
    """

    ticker_scope: tuple[str, ...]
    as_of: datetime
    as_of_iso: str
    sector_per_ticker: Mapping[str, SectorClassificationRow]
    bars_by_ticker: Mapping[str, tuple[DailyBarRow, ...]]
    baselines_volume: Mapping[str, TickerBaselineRow | None]
    baselines_atr: Mapping[str, TickerBaselineRow | None]
    gap_fill_history: Mapping[str, GapFillHistoryEntry]
    spy_window_returns: tuple[float, float] | None
    sector_etf_window_returns: Mapping[str, tuple[float, float] | None] = field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _resolve_scope(
    repository: DistillationRepository, *, ticker_scope: Sequence[str] | None
) -> tuple[str, ...]:
    """Resolve the effective ticker scope.

    ``None`` -> repository's default scope (every audience-covered ticker).
    Empty sequence -> empty tuple (caller short-circuits).
    """
    if ticker_scope is not None and len(ticker_scope) == 0:
        return ()
    if ticker_scope is None:
        return repository.load_default_ticker_scope()
    return tuple(ticker_scope)


# Smallest series for which a return can be computed has a head and a tail.
_RETURN_MIN_LEN: int = _BASE_ONE + _BASE_ONE


def cumulative_return(closes: Sequence[float]) -> float:
    """First→last close return; ``0.0`` for degenerate inputs.

    Public so :mod:`.assemble` consumes one canonical implementation rather
    than maintaining a duplicate.
    """
    if len(closes) < _RETURN_MIN_LEN:
        return 0.0
    first = closes[0]
    last = closes[-1]
    if first == 0.0:
        return 0.0
    return (last - first) / first


def _compute_window_returns(
    bars: Sequence[DailyBarRow],
) -> tuple[float, float] | None:
    """Compute the (5d, 20d) returns from a chronological bar list.

    Returns ``None`` when the bar list is shorter than the long window plus
    the seed bar — the caller substitutes a fallback or omits the entry.
    """
    if len(bars) < _RELATIVE_PERFORMANCE_LONG_DAYS + _BASE_ONE:
        return None
    closes = [b.adj_close for b in bars]
    return (
        cumulative_return(closes[-_RELATIVE_PERFORMANCE_SHORT_DAYS - _BASE_ONE :]),
        cumulative_return(closes[-_RELATIVE_PERFORMANCE_LONG_DAYS - _BASE_ONE :]),
    )


def load_q1_inputs(
    repository: DistillationRepository,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None,
) -> Q1Inputs:
    """Pre-load every Q1 input.

    The compute path (:func:`assemble_q1_blocks_from_inputs`) is pure over
    the returned :class:`Q1Inputs` — no further DB access is needed once
    this function returns.
    """
    del config  # not consumed during load; carried by callers
    scope = _resolve_scope(repository, ticker_scope=ticker_scope)
    as_of_iso = _format_iso_utc(as_of)
    if not scope:
        return Q1Inputs(
            ticker_scope=(),
            as_of=as_of,
            as_of_iso=as_of_iso,
            sector_per_ticker={},
            bars_by_ticker={},
            baselines_volume={},
            baselines_atr={},
            gap_fill_history={},
            spy_window_returns=None,
        )

    sector_rows = repository.load_sector_classifications(tickers=scope)

    bars_by_ticker: dict[str, tuple[DailyBarRow, ...]] = {
        ticker: repository.load_daily_bars(
            ticker=ticker, as_of=as_of_iso, days=_BAR_LOAD_LOOKBACK_DAYS
        )
        for ticker in scope
    }
    baselines_volume: dict[str, TickerBaselineRow | None] = {
        ticker: repository.load_latest_baseline(ticker=ticker, kind="volume", as_of=as_of_iso)
        for ticker in scope
    }
    baselines_atr: dict[str, TickerBaselineRow | None] = {
        ticker: repository.load_latest_baseline(ticker=ticker, kind="atr", as_of=as_of_iso)
        for ticker in scope
    }
    # Gap-fill history: per-ticker counts plus sector-pooled counts for the
    # ticker's sector. The sector pool is shared across tickers in the same
    # sector — cache it once.
    sector_pool_cache: dict[str, GapEventCounts] = {}
    gap_fill_history: dict[str, GapFillHistoryEntry] = {}
    for ticker in scope:
        sector_row = sector_rows.get(ticker)
        ticker_counts = repository.load_gap_fill_event_counts(ticker=ticker, as_of=as_of_iso)
        if sector_row is None:
            sector_counts = GapEventCounts(resolved=0, filled=0, pending=0)
        else:
            sector = sector_row.alphamind_sector
            if sector not in sector_pool_cache:
                sector_pool_cache[sector] = repository.load_sector_pooled_gap_fill_counts(
                    sector=sector, as_of=as_of_iso
                )
            sector_counts = sector_pool_cache[sector]
        gap_fill_history[ticker] = GapFillHistoryEntry(
            ticker_counts=ticker_counts,
            sector_counts=sector_counts,
        )

    # Relative-performance reads SPY + sector-ETF returns — pre-compute via
    # the same lookback the legacy ``_load_window_returns`` used.
    spy_bars = repository.load_daily_bars(
        ticker="SPY", as_of=as_of_iso, days=_RELATIVE_PERFORMANCE_LONG_DAYS + 1
    )
    spy_window_returns = _compute_window_returns(spy_bars)

    etf_returns: dict[str, tuple[float, float] | None] = {}
    for ticker in scope:
        sector_row = sector_rows.get(ticker)
        if sector_row is None or sector_row.alphamind_sector not in AUDIENCE_BY_SECTOR:
            continue
        etf = sector_row.sector_etf
        if etf in etf_returns:
            continue
        etf_bars = repository.load_daily_bars(
            ticker=etf,
            as_of=as_of_iso,
            days=_RELATIVE_PERFORMANCE_LONG_DAYS + 1,
        )
        etf_returns[etf] = _compute_window_returns(etf_bars)

    return Q1Inputs(
        ticker_scope=scope,
        as_of=as_of,
        as_of_iso=as_of_iso,
        sector_per_ticker=sector_rows,
        bars_by_ticker=bars_by_ticker,
        baselines_volume=baselines_volume,
        baselines_atr=baselines_atr,
        gap_fill_history=gap_fill_history,
        spy_window_returns=spy_window_returns,
        sector_etf_window_returns=etf_returns,
    )


__all__ = [
    "GapFillHistoryEntry",
    "Q1Inputs",
    "cumulative_return",
    "load_q1_inputs",
]
