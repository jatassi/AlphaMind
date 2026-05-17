"""Structural Protocols over the broker-adapter query classes (ALP-494).

Extracts the as-built sync/async method surface of
:class:`~alphamind.execution.broker_adapter.queries.AccountStateQueries` and
:class:`~alphamind.execution.broker_adapter.corporate_actions_queries.CorporateActionsQueries`
into runtime-checkable Protocols. The scheduler's
``gather_phase1_inputs`` depends on these Protocols, not the concrete
classes, so the debug-e2e package can substitute log-only stand-ins (story
02b) without monkey-patching module-level factory seams (story 03b's
``_build_*`` hooks).

Only the methods ``gather_phase1_inputs`` consumes go into each Protocol
surface (P9 — small Protocol surface).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from alpaca.data.enums import CorporateActionsType
from alpaca.data.models.corporate_actions import CorporateAction

from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)

__all__ = ["AccountStateQueriesP", "CorporateActionsQueriesP"]


@runtime_checkable
class AccountStateQueriesP(Protocol):
    """Sync read-only account/positions surface gather_phase1_inputs consumes.

    Mirrors the as-built methods on
    :class:`~alphamind.execution.broker_adapter.queries.AccountStateQueries`
    — both are synchronous because the underlying ``alpaca-py``
    ``TradingClient`` wraps httpx synchronously.
    """

    def get_account(self) -> TradeAccountSnapshot: ...

    def get_positions(self) -> tuple[PositionSnapshot, ...]: ...


@runtime_checkable
class CorporateActionsQueriesP(Protocol):
    """Async v1beta1 corporate-actions surface the Phase 1 fetcher consumes.

    Mirrors the as-built method on
    :class:`~alphamind.execution.broker_adapter.corporate_actions_queries.CorporateActionsQueries`.
    The underlying ``CorporateActionsClient`` is synchronous; the wrapper
    exposes ``async def`` so it composes with the surrounding
    ``InvocationContext`` substrate.
    """

    async def get_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...] | None = ...,
        start: date,
        end: date,
        types: tuple[CorporateActionsType, ...] = ...,
    ) -> tuple[CorporateAction, ...]: ...
