"""Tests for ``alphamind.execution.broker_adapter.protocols`` (ALP-494).

Confirms the structural Protocols are runtime-checkable and that the
concrete ``AccountStateQueries`` / ``CorporateActionsQueries`` classes
satisfy them via ``isinstance``. The Protocols are *extracted from* the
classes, so this is the acceptance check the story's contract rests on.
"""

from __future__ import annotations

from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.trading.client import TradingClient

from alphamind.execution.broker_adapter.corporate_actions_queries import (
    CorporateActionsQueries,
)
from alphamind.execution.broker_adapter.protocols import (
    AccountStateQueriesP,
    CorporateActionsQueriesP,
)
from alphamind.execution.broker_adapter.queries import (
    AccountStateQueries,
)


def test_account_state_queries_satisfies_protocol() -> None:
    client = TradingClient(api_key="placeholder", secret_key="placeholder", paper=True)
    assert isinstance(AccountStateQueries(client), AccountStateQueriesP)


def test_corporate_actions_queries_satisfies_protocol() -> None:
    client = CorporateActionsClient(api_key="placeholder", secret_key="placeholder")
    assert isinstance(CorporateActionsQueries(client), CorporateActionsQueriesP)
