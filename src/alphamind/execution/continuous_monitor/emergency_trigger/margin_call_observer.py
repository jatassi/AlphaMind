"""Broker-backed ``MarginCallObserver`` implementation (ALP-453).

The emergency-trigger evaluator (story 04b / ALP-439) accepts a
:class:`MarginCallObserver` protocol with :class:`NoMarginCallObserver` as
the test-only default. Production wires this implementation so the
margin-call trigger fires from live broker state instead of being suppressed.

The observer polls the broker's ``GET /v2/account`` endpoint on each tick
via :meth:`AccountStateQueries.get_account` and surfaces a
:class:`MarginCallEvent` when the account is in a maintenance-margin call
state: ``equity < maintenance_margin``. Equality is the solvent boundary
per Alpaca's maintenance-margin contract.

Stateless — each call reads fresh broker state. The retry/backoff envelope
is the caller's concern (the supervisor's loop already isolates per-tick
failures; a single failed broker call surfaces as the tick's exception and
the next tick re-probes).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from alphamind.execution.broker_adapter.queries import TradeAccountSnapshot
from alphamind.risk_guardrails.breach_behavior import MarginCallEvent

__all__ = ["AccountQueriesProtocol", "AlpacaMarginCallObserver"]


class AccountQueriesProtocol(Protocol):
    """Narrow surface the observer reads.

    :class:`AccountStateQueries` from ``broker_adapter.queries`` satisfies
    this. Carving the Protocol lets tests substitute a fake without
    constructing a live :class:`TradingClient`.
    """

    def get_account(self) -> TradeAccountSnapshot: ...


class AlpacaMarginCallObserver:
    """``MarginCallObserver`` backed by ``GET /v2/account``.

    Returns a :class:`MarginCallEvent` when the live account snapshot's
    ``maintenance_margin`` strictly exceeds ``equity``. The deficit
    (``maintenance_margin - equity``) populates
    :attr:`MarginCallEvent.additional_margin_required_usd`.
    """

    def __init__(
        self,
        *,
        queries: AccountQueriesProtocol,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._queries = queries
        self._now = now

    async def __call__(self) -> MarginCallEvent | None:
        # ``AccountStateQueries.get_account`` wraps a blocking HTTPX call;
        # offloading to a thread keeps the supervisor's event loop responsive
        # to the underlying-stream / fill-stream / greeks-refresh tasks.
        snapshot = await asyncio.to_thread(self._queries.get_account)
        deficit = snapshot.maintenance_margin - snapshot.equity
        if deficit <= 0.0:
            return None
        return MarginCallEvent(
            issued_at=self._now(),
            additional_margin_required_usd=deficit,
        )
