"""Per-ticker realized-vol substrate (ALP-530).

Stands up the missing producer that supplies a per-underlying scalar
realized volatility to the IV-fallback consumer (``IvProvider``
implementations in ``risk_guardrails/guardrail_evaluation/iv_sourcing.py``
— both the ``FixtureIvProvider`` for tests/bootstrap and the
production-side ``SqlOptionsIvProvider`` (ALP-642) that reads
``options_contract_snapshots`` for surface hits and falls through here
on miss).

Three deliverables share this module:

1. :func:`compute_trailing_realized_vol` — the pure compute over an
   ascending close-price tuple. No I/O. Mirrors the formula
   ``_realized_vols_from_log_returns`` uses in the orchestrator's
   regime block (``std(log_returns, ddof=1) * sqrt(annualization)``)
   but at per-ticker granularity and configurable horizon.
2. :func:`persist_per_ticker_realized_vol` — the per-invocation
   imperative-shell wrapper that writes one row per ticker into
   ``ticker_realized_vol``. Sync to match the distillation orchestrator's
   own sync ``Session`` (the orchestrator runs under ``asyncio.to_thread``).
3. :func:`read_realized_vol_map` — the consumer-side read returning
   ``dict[str, float]`` (per-ticker trailing 30d realized vol). Each
   wiring site wraps the scalars into ``RealizedVolEntry`` objects at
   the consumer boundary, preserving the one-way import direction
   (``risk_guardrails`` consumes this module; never the reverse).

Architectural invariants (per ALP-130 orchestrator notes):
- This module must NOT import from ``risk_guardrails/``, ``execution/``,
  or ``scheduler/``. The wiring sites in those packages import the read
  function — the dependency arrow points downward.
- The compute is pure. No module-level config imports; defaults are
  function parameters.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from alphamind.distillation.q7._helpers_compute import _log_returns_from_closes
from alphamind.persistence.models import TickerRealizedVolRow

__all__ = [
    "compute_trailing_realized_vol",
    "persist_per_ticker_realized_vol",
    "read_realized_vol_map",
]


def _format_iso_utc(dt: datetime) -> str:
    """Render a tz-aware datetime as ISO-8601 UTC with a ``Z`` suffix.

    Mirrors ``q7._helpers._format_iso_utc`` to keep the timestamp encoding
    consistent across the distillation layer.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Default minimum number of log-return observations for a valid estimate.
# Five is the smallest sample that yields a non-degenerate sample-std under
# ``ddof=1`` while still admitting an estimate from a ~one-trading-week
# window. The caller controls the horizon by passing more or fewer closes;
# this floor only gates against catastrophically thin data.
_DEFAULT_MIN_RETURNS: int = 5

# Standard U.S. trading-days-per-year annualization factor — matches the
# orchestrator's regime-block convention (``_TRADING_DAYS_PER_YEAR = 252``).
_DEFAULT_ANNUALIZATION_FACTOR: float = 252.0


def compute_trailing_realized_vol(
    closes: tuple[float, ...],
    *,
    min_returns: int = _DEFAULT_MIN_RETURNS,
    annualization_factor: float = _DEFAULT_ANNUALIZATION_FACTOR,
) -> float | None:
    """Annualized realized volatility from an ascending close-price tuple.

    Returns ``std(log_returns, ddof=1) * sqrt(annualization_factor)`` —
    the sample-std convention matching the ``RealizedVolEntry`` docstring's
    spec (``std(daily log returns) * sqrt(252)``). Returns ``None`` when
    fewer than ``min_returns`` log returns can be produced from ``closes``
    (i.e. when ``len(closes) < min_returns + 1``).

    Pure: no I/O, no config lookups. The caller decides the horizon by
    choosing how many closes to pass. ``min_returns`` and
    ``annualization_factor`` are defaults; the persister passes 30 trading
    days of closes via the ``lookback_days`` parameter.
    """
    log_returns = _log_returns_from_closes(closes)
    if len(log_returns) < min_returns:
        return None
    return float(statistics.stdev(log_returns) * math.sqrt(annualization_factor))


# ---------------------------------------------------------------------------
# Per-invocation persister
# ---------------------------------------------------------------------------


# Default number of trailing trading-day closes the persister consumes. 30
# matches the ``RealizedVolEntry.trailing_30d_realized_vol`` field name; the
# caller may override for backfill / experimentation paths.
_DEFAULT_LOOKBACK_DAYS: int = 30


def persist_per_ticker_realized_vol(
    session: Session,
    *,
    invocation_id: str,
    tickers: Iterable[str],
    closes_by_ticker: Mapping[str, tuple[float, ...]],
    as_of_date: date,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    computed_at: datetime | None = None,
) -> int:
    """Persist one realized-vol row per ticker into ``ticker_realized_vol``.

    Iterates ``tickers``; for each, reads the trailing ``lookback_days``
    closes from ``closes_by_ticker[ticker]`` and calls
    :func:`compute_trailing_realized_vol`. Tickers absent from the closes
    map or with insufficient history (``None`` from the compute) are
    silently skipped — the read path falls back to whatever rows are
    present.

    Same-day re-invocations upsert on the composite PK
    ``(ticker, as_of_date)``: an existing row's ``trailing_30d_realized_vol``,
    ``invocation_id``, and ``computed_at`` are overwritten in place.

    All writes join the caller's session/transaction — surrounding
    ``InvocationContext`` commits on clean exit. Sync to match the
    distillation orchestrator's sync ``Session`` (the orchestrator runs
    under ``asyncio.to_thread``).

    Returns the count of rows written or updated.
    """
    if computed_at is None:
        computed_at = datetime.now(UTC)
    computed_at_iso = _format_iso_utc(computed_at)
    as_of_iso = as_of_date.isoformat()

    written = 0
    for ticker in tickers:
        closes = closes_by_ticker.get(ticker)
        if closes is None:
            continue
        trimmed = closes[-lookback_days:] if len(closes) > lookback_days else closes
        vol = compute_trailing_realized_vol(trimmed)
        if vol is None:
            continue

        existing = session.execute(
            select(TickerRealizedVolRow).where(
                TickerRealizedVolRow.ticker == ticker,
                TickerRealizedVolRow.as_of_date == as_of_iso,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                TickerRealizedVolRow(
                    ticker=ticker,
                    as_of_date=as_of_iso,
                    trailing_30d_realized_vol=vol,
                    invocation_id=invocation_id,
                    computed_at=computed_at_iso,
                )
            )
        else:
            existing.trailing_30d_realized_vol = vol
            existing.invocation_id = invocation_id
            existing.computed_at = computed_at_iso
        written += 1

    return written


# ---------------------------------------------------------------------------
# Consumer-side read
# ---------------------------------------------------------------------------


async def read_realized_vol_map(
    session: AsyncSession,
    *,
    tickers: Iterable[str] | None = None,
    as_of_date: date | None = None,
) -> dict[str, float]:
    """Read the per-ticker trailing-30d realized-vol map.

    Returns ``dict[str, float]`` keyed on ticker. When ``as_of_date`` is
    ``None`` (the production default), the latest row per ticker is
    returned (joined against a per-ticker ``MAX(as_of_date)`` subquery).
    When supplied, only rows pinned to that ``as_of_date`` are returned;
    an absent date yields the empty dict.

    When ``tickers`` is ``None`` every ticker present in the table is
    included; otherwise the result is filtered to the requested set.

    The float values are the persisted ``trailing_30d_realized_vol``
    scalars. Wiring sites that consume ``RealizedVolEntry``-shaped
    mappings convert at the boundary — this module is forbidden from
    importing the consumer-side dataclass to preserve the one-way
    dependency arrow (the wiring sites in ``risk_guardrails`` /
    ``execution`` / ``scheduler`` consume this module, never the
    reverse).
    """
    ticker_filter: tuple[str, ...] | None = None if tickers is None else tuple(tickers)
    if ticker_filter is not None and not ticker_filter:
        # Explicit empty filter — nothing to fetch.
        return {}

    if as_of_date is None:
        # Latest-per-ticker subquery: join on the per-ticker MAX(as_of_date).
        latest_subq = (
            select(
                TickerRealizedVolRow.ticker.label("ticker"),
                func.max(TickerRealizedVolRow.as_of_date).label("latest"),
            )
            .group_by(TickerRealizedVolRow.ticker)
            .subquery()
        )
        stmt = select(
            TickerRealizedVolRow.ticker,
            TickerRealizedVolRow.trailing_30d_realized_vol,
        ).join(
            latest_subq,
            (TickerRealizedVolRow.ticker == latest_subq.c.ticker)
            & (TickerRealizedVolRow.as_of_date == latest_subq.c.latest),
        )
    else:
        stmt = select(
            TickerRealizedVolRow.ticker,
            TickerRealizedVolRow.trailing_30d_realized_vol,
        ).where(TickerRealizedVolRow.as_of_date == as_of_date.isoformat())

    if ticker_filter is not None:
        stmt = stmt.where(TickerRealizedVolRow.ticker.in_(ticker_filter))

    rows = (await session.execute(stmt)).all()
    return {str(ticker): float(vol) for ticker, vol in rows}
