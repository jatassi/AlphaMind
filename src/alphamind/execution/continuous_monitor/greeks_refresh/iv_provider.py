"""IV reader against the collector-populated options-chains table (story 03a).

Parent issue ALP-123 § Pre-resolved decision (C): the IV input to greeks
recomputation is sourced from the collector's
``options_contract_snapshots`` table (informally "options chains") rather
than a vendor-direct pull. The collector's ``polygon.options`` runner
writes a row every 30 minutes during market hours; the monitor reads the
latest snapshot per OCC contract symbol.

Per the design's "fail-closed on missing data" posture: a missing IV is
treated as a fetch failure that triggers retry; an exhausted retry budget
preserves the prior greeks and surfaces the failure to the activity log
(see :mod:`alphamind.execution.continuous_monitor.greeks_refresh.task`).

Schema verification: the table's IV column is ``implied_volatility`` (real,
nullable), keyed via ``contract_ticker`` (the OCC symbol the collector
emits). Cross-referenced 2026-05-11 against
``src/alphamind/data_sources/polygon/options.py`` and
``docs/design/01-data-layer/collector/storage.md`` § Options chains.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.persistence.models import OptionsContractSnapshots


@dataclass(frozen=True, slots=True)
class IVQuote:
    """One row's worth of IV + snapshot timestamp for an OCC contract.

    ``as_of`` is tz-aware UTC — mirrors :class:`UnderlyingQuote` so the
    refresh task can reason about freshness uniformly. The collector writes
    the snapshot timestamp in ISO 8601 with the project-wide ``Z``
    convention; the reader normalizes to ``datetime(tzinfo=UTC)``.
    """

    occ_symbol: str
    iv: float
    as_of: datetime


def _parse_snapshot_ts(raw: str) -> datetime:
    """Parse the collector's snapshot timestamp string into a tz-aware UTC datetime.

    The collector writes ``datetime.now(UTC).isoformat()`` which produces
    a ``+00:00`` suffix; older bootstrap rows may carry a bare ``Z``. Both
    parse cleanly via ``datetime.fromisoformat`` on Python 3.11+. Naive
    strings (an unexpected hand-crafted row) are normalized by attaching
    UTC so the contract holds.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def fetch_iv_from_options_chains(
    session: AsyncSession,
    *,
    occ_symbol: str,
) -> IVQuote | None:
    """Return the latest ``implied_volatility`` row for *occ_symbol* or ``None``.

    Reads the row with the largest ``snapshot_ts`` for the given
    ``contract_ticker`` and a non-NULL ``implied_volatility``. Returns
    ``None`` when no such row exists — the typical case is a freshly-opened
    position whose collector cron has not yet snapshot the chain, or a
    contract the collector has stopped tracking after expiration.

    The reader does NOT raise on missing-row: it returns ``None`` so the
    caller can distinguish "no data yet" from a database error.
    """
    stmt = (
        select(
            OptionsContractSnapshots.snapshot_ts,
            OptionsContractSnapshots.implied_volatility,
        )
        .where(
            OptionsContractSnapshots.contract_ticker == occ_symbol,
            OptionsContractSnapshots.implied_volatility.is_not(None),
        )
        .order_by(OptionsContractSnapshots.snapshot_ts.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    snapshot_ts_str, iv_value = row
    if iv_value is None:
        return None
    return IVQuote(
        occ_symbol=occ_symbol,
        iv=float(iv_value),
        as_of=_parse_snapshot_ts(snapshot_ts_str),
    )


async def fetch_iv_quotes_batch(
    session: AsyncSession,
    *,
    occ_symbols: Iterable[str],
) -> dict[str, IVQuote]:
    """Batched variant: one query for many OCC symbols (efficiency lens).

    Returns a mapping ``{occ_symbol: IVQuote}`` populated only for symbols
    that resolved to a non-NULL ``implied_volatility`` row. Missing symbols
    are simply absent from the result — the caller iterates and routes
    misses through the per-symbol retry / activity-log failure path.

    The query selects the latest snapshot per ``contract_ticker`` via a
    correlated subquery on ``MAX(snapshot_ts)`` so a single round-trip
    covers an entire refresh cycle's worth of due positions, regardless of
    how stale the collector's last full chain pull is.
    """
    symbols = tuple({sym for sym in occ_symbols if sym})
    if not symbols:
        return {}

    # Subquery: per OCC symbol, the max snapshot_ts where IV is non-NULL.
    latest_subq = (
        select(
            OptionsContractSnapshots.contract_ticker.label("ct"),
            func.max(OptionsContractSnapshots.snapshot_ts).label("max_ts"),
        )
        .where(
            OptionsContractSnapshots.contract_ticker.in_(symbols),
            OptionsContractSnapshots.implied_volatility.is_not(None),
        )
        .group_by(OptionsContractSnapshots.contract_ticker)
        .subquery()
    )

    stmt = select(
        OptionsContractSnapshots.contract_ticker,
        OptionsContractSnapshots.snapshot_ts,
        OptionsContractSnapshots.implied_volatility,
    ).join(
        latest_subq,
        (OptionsContractSnapshots.contract_ticker == latest_subq.c.ct)
        & (OptionsContractSnapshots.snapshot_ts == latest_subq.c.max_ts),
    )

    result = await session.execute(stmt)
    out: dict[str, IVQuote] = {}
    for row in result.all():
        ct, snapshot_ts_str, iv_value = row
        if iv_value is None:
            continue
        out[ct] = IVQuote(
            occ_symbol=ct,
            iv=float(iv_value),
            as_of=_parse_snapshot_ts(snapshot_ts_str),
        )
    return out
