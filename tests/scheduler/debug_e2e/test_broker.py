"""Tests for the debug-e2e log-only broker stand-ins (story 02b / ALP-498).

Covers:

* ``LogOnlyAccountStateQueries`` and ``LogOnlyCorporateActionsQueries``
  structurally satisfy the broker-adapter Protocols (``isinstance`` against
  ``AccountStateQueriesP`` / ``CorporateActionsQueriesP``).
* Return shapes — ``get_account()`` yields a ``TradeAccountSnapshot``,
  ``get_positions()`` yields one ``PositionSnapshot`` per synthetic position,
  ``get_corporate_actions(...)`` yields ``()``.
* Each method emits one INFO log line prefixed ``[debug_e2e]`` capturing
  the method name and the key call args.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.execution.broker_adapter.protocols import (
    AccountStateQueriesP,
    CorporateActionsQueriesP,
)
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.scheduler.debug_e2e.broker import (
    LogOnlyAccountStateQueries,
    LogOnlyCorporateActionsQueries,
)
from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO

# ---------------------------------------------------------------------------
# (a) Protocol satisfaction — runtime ``isinstance`` checks
# ---------------------------------------------------------------------------


class TestProtocolSatisfaction:
    def test_log_only_account_queries_satisfies_protocol(self) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        assert isinstance(queries, AccountStateQueriesP)

    def test_log_only_corporate_actions_queries_satisfies_protocol(self) -> None:
        queries = LogOnlyCorporateActionsQueries()
        assert isinstance(queries, CorporateActionsQueriesP)


# ---------------------------------------------------------------------------
# (b) Return shapes — account / positions / corporate actions
# ---------------------------------------------------------------------------


class TestAccountSnapshotShape:
    def test_get_account_returns_trade_account_snapshot(self) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        snapshot = queries.get_account()
        assert isinstance(snapshot, TradeAccountSnapshot)

    def test_get_account_cash_matches_starting_cash(self) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        snapshot = queries.get_account()
        # Cash on the snapshot is Decimal-backed; compare numerically.
        assert float(snapshot.cash) == SYNTHETIC_PORTFOLIO.starting_cash_usd

    def test_get_account_equity_includes_starting_cash(self) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        snapshot = queries.get_account()
        # Equity should be at least starting cash (positions are entered at
        # cost so portfolio value adds equity on top).
        assert float(snapshot.equity) >= SYNTHETIC_PORTFOLIO.starting_cash_usd


class TestPositionsShape:
    def test_get_positions_returns_tuple_of_position_snapshots(self) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        positions = queries.get_positions()
        assert isinstance(positions, tuple)
        # One snapshot per synthetic position (8).
        assert len(positions) == len(SYNTHETIC_PORTFOLIO.positions)
        for pos in positions:
            assert isinstance(pos, PositionSnapshot)

    def test_short_position_marked_short_side(self) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        positions = queries.get_positions()
        tsla = next(p for p in positions if p.symbol == Symbol("TSLA"))
        assert tsla.side == "short"

    def test_long_equity_marked_long_side(self) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        positions = queries.get_positions()
        nvda = next(p for p in positions if p.symbol == Symbol("NVDA"))
        assert nvda.side == "long"


class TestCorporateActionsEmpty:
    def test_get_corporate_actions_returns_empty_tuple(self) -> None:
        queries = LogOnlyCorporateActionsQueries()
        result = asyncio.run(
            queries.get_corporate_actions(
                symbols=("NVDA", "JPM"),
                start=date(2026, 5, 1),
                end=date(2026, 5, 17),
            )
        )
        assert result == ()


# ---------------------------------------------------------------------------
# (c) Logging — each method emits one INFO line prefixed [debug_e2e]
# ---------------------------------------------------------------------------


_LOGGER_NAME = "alphamind.scheduler.debug_e2e.broker"


class TestLogging:
    def test_get_account_emits_one_debug_e2e_info_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            queries.get_account()
        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        msg = records[0].getMessage()
        assert "[debug_e2e]" in msg
        assert "get_account" in msg

    def test_get_positions_emits_one_debug_e2e_info_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        queries = LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO)
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            queries.get_positions()
        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        msg = records[0].getMessage()
        assert "[debug_e2e]" in msg
        assert "get_positions" in msg

    def test_get_corporate_actions_emits_one_debug_e2e_info_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        queries = LogOnlyCorporateActionsQueries()
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            asyncio.run(
                queries.get_corporate_actions(
                    symbols=("NVDA", "JPM"),
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 17),
                )
            )
        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        msg = records[0].getMessage()
        assert "[debug_e2e]" in msg
        assert "get_corporate_actions" in msg
        # Key call args appear in the message (start, end, symbol count).
        assert "2026-05-01" in msg
        assert "2026-05-17" in msg
