"""Log-only broker-adapter stand-ins for ``--debug-e2e`` mode (story 02b / ALP-498).

Two classes satisfying the broker-adapter Protocols structurally (no
inheritance from the Alpaca-backed production classes):

* :class:`LogOnlyAccountStateQueries` — returns ``TradeAccountSnapshot`` /
  ``PositionSnapshot`` records derived deterministically from
  :data:`SYNTHETIC_PORTFOLIO`.
* :class:`LogOnlyCorporateActionsQueries` — returns ``()`` (no CAs on the
  synthetic portfolio).

Both classes log every method call at INFO with a ``[debug_e2e]`` prefix
so the operator can confirm the substitution is in effect when reviewing
the run log.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from alpaca.data.enums import CorporateActionsType
from alpaca.data.models.corporate_actions import CorporateAction

from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.broker_adapter.entry_pricing import TouchQuote
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.portfolio_state.records.positions import Direction
from alphamind.scheduler.debug_e2e.portfolio import (
    SyntheticEquity,
    SyntheticOption,
    SyntheticPortfolio,
    SyntheticPosition,
)

__all__ = [
    "LogOnlyAccountStateQueries",
    "LogOnlyBatchQuoteSource",
    "LogOnlyCorporateActionsQueries",
]

log = logging.getLogger(__name__)

# Options contract multiplier — 100 shares per contract on listed US options.
_OPTION_MULTIPLIER = Decimal(100)


def _position_cost_basis(position: SyntheticPosition) -> Decimal:
    """Cost basis of a single synthetic position in USD.

    Equities: ``qty * avg_cost`` (sign-flipped for shorts).
    Options : ``contracts * premium * 100``.
    Strategies: ``net_premium * 100`` (per-spread debit/credit).
    """
    if isinstance(position, SyntheticEquity):
        gross = Decimal(str(position.qty)) * Decimal(str(position.avg_cost))
        return -gross if position.direction is Direction.SHORT else gross
    if isinstance(position, SyntheticOption):
        return (
            Decimal(str(position.contracts))
            * Decimal(str(position.premium_per_contract))
            * _OPTION_MULTIPLIER
        )
    return Decimal(str(position.net_premium)) * _OPTION_MULTIPLIER


def _position_snapshot(position: SyntheticPosition) -> PositionSnapshot:
    """Project a synthetic position into the broker-adapter snapshot shape."""
    if isinstance(position, SyntheticEquity):
        is_short = position.direction is Direction.SHORT
        qty = position.qty
        cost = Decimal(str(qty)) * Decimal(str(position.avg_cost))
        signed_cost = -cost if is_short else cost
        return PositionSnapshot(
            symbol=str(position.symbol),
            asset_class="us_equity",
            qty=qty,
            avg_entry_price=price(Decimal(str(position.avg_cost))),
            market_value=signed_money(signed_cost),
            cost_basis=signed_money(signed_cost),
            unrealized_pl=signed_money(Decimal(0)),
            unrealized_plpc=0.0,
            current_price=price(Decimal(str(position.avg_cost))),
            side="short" if is_short else "long",
        )

    if isinstance(position, SyntheticOption):
        cost = (
            Decimal(str(position.contracts))
            * Decimal(str(position.premium_per_contract))
            * _OPTION_MULTIPLIER
        )
        return PositionSnapshot(
            symbol=str(position.underlying),
            asset_class="us_option",
            qty=position.contracts,
            avg_entry_price=price(Decimal(str(position.premium_per_contract))),
            market_value=signed_money(cost),
            cost_basis=signed_money(cost),
            unrealized_pl=signed_money(Decimal(0)),
            unrealized_plpc=0.0,
            current_price=price(Decimal(str(position.premium_per_contract))),
            side="long",
        )

    # SyntheticStrategy — single composite snapshot keyed by the underlying.
    # The canonical fixture is a net-debit spread; a future net-credit
    # variant will need a sign convention here before it can be projected.
    entry_price_value = Decimal(str(position.net_premium))
    net_cost = entry_price_value * _OPTION_MULTIPLIER
    return PositionSnapshot(
        symbol=str(position.underlying),
        asset_class="us_option",
        qty=1.0,
        avg_entry_price=price(entry_price_value),
        market_value=signed_money(net_cost),
        cost_basis=signed_money(net_cost),
        unrealized_pl=signed_money(Decimal(0)),
        unrealized_plpc=0.0,
        current_price=price(entry_price_value),
        side="long",
    )


class LogOnlyAccountStateQueries:
    """Log-only ``AccountStateQueriesP`` stand-in for ``--debug-e2e``.

    Returns deterministic ``TradeAccountSnapshot`` / ``PositionSnapshot``
    records derived from the held :class:`SyntheticPortfolio`; logs every
    call at INFO with a ``[debug_e2e]`` prefix.
    """

    def __init__(self, portfolio: SyntheticPortfolio) -> None:
        self._portfolio = portfolio

    def get_account(self) -> TradeAccountSnapshot:
        log.info("[debug_e2e] LogOnlyAccountStateQueries.get_account()")
        cash = Decimal(str(self._portfolio.starting_cash_usd))
        portfolio_value = sum(
            (_position_cost_basis(p) for p in self._portfolio.positions),
            start=Decimal(0),
        )
        equity = cash + portfolio_value
        zero = Decimal(0)
        return TradeAccountSnapshot(
            account_id="debug-e2e-synthetic-account",
            cash=money(cash),
            equity=money(equity),
            buying_power=money(cash),
            regt_buying_power=money(cash),
            daytrading_buying_power=money(cash),
            maintenance_margin=money(zero),
            daytrade_count=0,
            pattern_day_trader=False,
            status="ACTIVE",
        )

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        log.info("[debug_e2e] LogOnlyAccountStateQueries.get_positions()")
        return tuple(_position_snapshot(p) for p in self._portfolio.positions)


class LogOnlyCorporateActionsQueries:
    """Log-only ``CorporateActionsQueriesP`` stand-in for ``--debug-e2e``.

    Returns ``()`` — the synthetic portfolio has no corporate actions —
    and logs the call at INFO with a ``[debug_e2e]`` prefix.
    """

    async def get_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...] | None = None,
        start: date,
        end: date,
        types: tuple[CorporateActionsType, ...] = (),
    ) -> tuple[CorporateAction, ...]:
        log.info(
            "[debug_e2e] LogOnlyCorporateActionsQueries.get_corporate_actions("
            "start=%s end=%s symbols=%s types=%d)",
            start.isoformat(),
            end.isoformat(),
            # ``symbols is None`` means "all symbols" per the production
            # corporate-actions fetcher's semantics; surface that
            # explicitly rather than logging an ambiguous ``0``.
            "<all>" if symbols is None else len(symbols),
            len(types),
        )
        return ()


class LogOnlyBatchQuoteSource:
    """Log-only ``BatchQuoteSource`` stand-in for ``--debug-e2e`` (ALP-753).

    Returns ``{}`` — no live quotes — so the phase-1 reference layer falls back
    deterministically to the seeded ``ohlcv_bars`` for every active ticker, the
    same offline, reproducible bar-based behavior debug-e2e had before ALP-753.
    Returning an empty map (rather than raising) keeps ``staleness_flag`` off, so
    a debug-e2e run is not spuriously marked degraded. Logs the call at INFO with
    a ``[debug_e2e]`` prefix so the operator can confirm no live fetch occurred.
    """

    async def latest_quotes(self, symbols: Sequence[str]) -> Mapping[str, TouchQuote]:
        log.info(
            "[debug_e2e] LogOnlyBatchQuoteSource.latest_quotes(%d symbol(s)) "
            "-> {} (offline; active universe falls back to seeded bars)",
            len(list(symbols)),
        )
        return {}
