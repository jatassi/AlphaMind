"""``SqlOptionPriceProvider`` — :class:`OptionPriceProvider` backed by SQLAlchemy.

Reads the most recent row per ``contract_ticker`` from
``options_contract_snapshots`` — the same table the continuous monitor's
greeks-refresh task feeds (see
:mod:`alphamind.execution.continuous_monitor.greeks_refresh.iv_provider`).
Per-cycle reads use a fresh sync ``Session`` derived from the async
session factory's engine URL, mirroring
:class:`SqlPortfolioStateRepository`. WAL mode permits the concurrent
reader; the collector's writes land atomically per ``(snapshot_ts,
contract_ticker)`` so the assembler and the greeks path see a consistent
snapshot of each contract.

Price selection: bid/ask midpoint when both legs are positive (industry
standard for mark-to-market); ``last_price`` is the secondary fallback;
absent both, the symbol is omitted from the result and the assembler
falls back to ``premium_paid_per_contract``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.persistence.models import OptionsContractSnapshots
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    UnknownOptionContractError,
)
from alphamind.state.repository.sql_repository import _build_sync_session_factory


def _parse_snapshot_ts(raw: str) -> datetime:
    """Parse the collector's snapshot timestamp into a tz-aware UTC datetime.

    Mirrors
    :func:`alphamind.execution.continuous_monitor.greeks_refresh.iv_provider._parse_snapshot_ts`
    — kept local rather than imported to avoid an upward edge into the
    continuous-monitor package.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _select_option_price(
    *,
    last_price: float | None,
    bid: float | None,
    ask: float | None,
) -> float | None:
    """Pick the option price from a snapshot row.

    Industry-standard MTM uses the bid/ask midpoint when both legs are
    positive and the book is not crossed (bid <= ask) — quote-driven, robust
    to trade staleness. Falls back to ``last_price`` when a usable midpoint
    is unavailable (zero/crossed/locked quote, or a missing leg). Returns
    ``None`` when neither candidate is usable so the caller can omit the
    symbol; the assembler then keeps the entry-premium fallback.

    A zero bid alone is treated as no-quote (deep-OTM contracts often quote
    bid=0); the caller fall through to ``last_price`` and only omits the
    symbol when both candidates are absent.
    """
    if bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask:
        return (bid + ask) / 2.0
    if last_price is not None and last_price > 0:
        return last_price
    return None


class SqlOptionPriceProvider:
    """SQLAlchemy-backed :class:`OptionPriceProvider` against ``options_contract_snapshots``.

    One fresh sync ``Session`` per ``get_quotes`` call (matching the
    repository's pattern); the underlying engine is shared with the async
    write path so WAL-mode SQLite allows concurrent readers.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sync_session_factory = _build_sync_session_factory(session_factory)
        self._now = now

    def get_quote(
        self,
        occ_symbol: str,
        *,
        freshness_threshold_seconds: float,
    ) -> PriceQuote:
        result = self.get_quotes(
            (occ_symbol,), freshness_threshold_seconds=freshness_threshold_seconds
        )
        if occ_symbol not in result:
            raise UnknownOptionContractError(occ_symbol)
        return result[occ_symbol]

    def get_quotes(
        self,
        occ_symbols: tuple[str, ...],
        *,
        freshness_threshold_seconds: float,
    ) -> dict[str, PriceQuote]:
        unique = tuple({s for s in occ_symbols if s})
        if not unique:
            return {}

        # Latest snapshot row per ``contract_ticker`` via correlated subquery
        # on ``MAX(snapshot_ts)`` — single round-trip even when the assembler
        # needs to mark many contracts at once.
        latest_subq = (
            select(
                OptionsContractSnapshots.contract_ticker.label("ct"),
                func.max(OptionsContractSnapshots.snapshot_ts).label("max_ts"),
            )
            .where(OptionsContractSnapshots.contract_ticker.in_(unique))
            .group_by(OptionsContractSnapshots.contract_ticker)
            .subquery()
        )
        stmt = select(
            OptionsContractSnapshots.contract_ticker,
            OptionsContractSnapshots.snapshot_ts,
            OptionsContractSnapshots.last_price,
            OptionsContractSnapshots.bid,
            OptionsContractSnapshots.ask,
        ).join(
            latest_subq,
            (OptionsContractSnapshots.contract_ticker == latest_subq.c.ct)
            & (OptionsContractSnapshots.snapshot_ts == latest_subq.c.max_ts),
        )

        out: dict[str, PriceQuote] = {}
        now = self._now()
        with self._sync_session_factory() as session:
            result = session.execute(stmt)
            for ct, snapshot_ts_str, last_price, bid, ask in result.all():
                price = _select_option_price(last_price=last_price, bid=bid, ask=ask)
                if price is None:
                    continue
                as_of = _parse_snapshot_ts(snapshot_ts_str)
                is_stale = (now - as_of).total_seconds() > freshness_threshold_seconds
                out[ct] = PriceQuote(
                    ticker=ct,
                    price_usd=price,
                    as_of_timestamp=as_of,
                    source=PriceSource.INTRADAY_QUOTE,
                    is_stale=is_stale,
                )
        return out


__all__ = ["SqlOptionPriceProvider"]
