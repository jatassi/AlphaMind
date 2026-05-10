"""Tests for the v1beta1 ``CorporateActionsQueries`` thin wrapper (ALP-410).

The wrapper exists so the fetcher can pass an awaitable + typed-tuple surface
to the rest of the corporate-actions package without binding the fetcher to
alpaca-py's sync client + per-action-type-keyed dict response.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.models.corporate_actions import (
    CashDividend,
    CorporateActionsSet,
    ForwardSplit,
    NameChange,
)
from alpaca.data.requests import CorporateActionsRequest


def _forward_split(symbol: str, ex_date: date, seed: int) -> ForwardSplit:
    return ForwardSplit(
        id=UUID(int=seed),
        corporate_action_type="forward_split",
        symbol=symbol,
        cusip="037833100",
        new_rate=4.0,
        old_rate=1.0,
        process_date=ex_date,
        ex_date=ex_date,
    )


def _cash_dividend(symbol: str, ex_date: date, seed: int) -> CashDividend:
    return CashDividend(
        id=UUID(int=seed),
        corporate_action_type="cash_dividend",
        symbol=symbol,
        cusip="037833100",
        rate=0.50,
        special=False,
        foreign=False,
        process_date=ex_date,
        ex_date=ex_date,
    )


def _name_change(old_symbol: str, new_symbol: str, process_date: date, seed: int) -> NameChange:
    return NameChange(
        id=UUID(int=seed),
        corporate_action_type="name_change",
        old_symbol=old_symbol,
        old_cusip="037833100",
        new_symbol=new_symbol,
        new_cusip="037833200",
        process_date=process_date,
    )


async def test_flattens_per_type_groups_into_a_single_tuple() -> None:
    """``CorporateActionsSet.data`` (dict-of-lists) is flattened into one tuple."""
    from alphamind.execution.broker_adapter.corporate_actions_queries import (
        CorporateActionsQueries,
    )

    ex = date(2026, 5, 5)
    events_set = CorporateActionsSet.model_construct(
        data={
            "forward_splits": [_forward_split("AAPL", ex, seed=1)],
            "cash_dividends": [_cash_dividend("AAPL", ex, seed=2)],
            "name_changes": [_name_change("OLD", "NEW", ex, seed=3)],
        }
    )

    client = MagicMock(spec=CorporateActionsClient)
    client.get_corporate_actions.return_value = events_set

    queries = CorporateActionsQueries(client)
    result = await queries.get_corporate_actions(
        symbols=("AAPL", "OLD"),
        start=date(2026, 5, 1),
        end=date(2026, 5, 8),
    )

    assert isinstance(result, tuple)
    assert len(result) == 3
    seeds = sorted(e.id.int for e in result)
    assert seeds == [1, 2, 3]


async def test_empty_set_returns_empty_tuple() -> None:
    """An empty ``CorporateActionsSet`` flattens to ``()``."""
    from alphamind.execution.broker_adapter.corporate_actions_queries import (
        CorporateActionsQueries,
    )

    client = MagicMock(spec=CorporateActionsClient)
    client.get_corporate_actions.return_value = CorporateActionsSet.model_construct(data={})

    queries = CorporateActionsQueries(client)
    result = await queries.get_corporate_actions(
        symbols=("AAPL",),
        start=date(2026, 5, 1),
        end=date(2026, 5, 8),
    )

    assert result == ()


async def test_passes_request_kwargs_through() -> None:
    """Request kwargs are translated into a ``CorporateActionsRequest``."""
    from alphamind.execution.broker_adapter.corporate_actions_queries import (
        CorporateActionsQueries,
    )

    client = MagicMock(spec=CorporateActionsClient)
    client.get_corporate_actions.return_value = CorporateActionsSet.model_construct(data={})

    queries = CorporateActionsQueries(client)
    await queries.get_corporate_actions(
        symbols=("AAPL", "MSFT"),
        start=date(2026, 5, 1),
        end=date(2026, 5, 8),
        types=(
            CorporateActionsType.FORWARD_SPLIT,
            CorporateActionsType.CASH_DIVIDEND,
        ),
    )

    assert client.get_corporate_actions.call_count == 1
    (request,), _ = client.get_corporate_actions.call_args
    assert isinstance(request, CorporateActionsRequest)
    assert request.symbols == ["AAPL", "MSFT"]
    assert request.start == date(2026, 5, 1)
    assert request.end == date(2026, 5, 8)
    assert request.types is not None
    assert set(request.types) == {
        CorporateActionsType.FORWARD_SPLIT,
        CorporateActionsType.CASH_DIVIDEND,
    }


async def test_default_types_exclude_out_of_scope_members() -> None:
    """When ``types`` is omitted, the request excludes out-of-scope event types."""
    from alphamind.execution.broker_adapter.corporate_actions_queries import (
        CorporateActionsQueries,
    )

    client = MagicMock(spec=CorporateActionsClient)
    client.get_corporate_actions.return_value = CorporateActionsSet.model_construct(data={})

    queries = CorporateActionsQueries(client)
    await queries.get_corporate_actions(
        symbols=("AAPL",),
        start=date(2026, 5, 1),
        end=date(2026, 5, 8),
    )

    (request,), _ = client.get_corporate_actions.call_args
    assert request.types is not None
    sent_types = set(request.types)
    assert CorporateActionsType.UNIT_SPLIT not in sent_types
    assert CorporateActionsType.REDEMPTION not in sent_types
    assert CorporateActionsType.WORTHLESS_REMOVAL not in sent_types
    assert CorporateActionsType.RIGHTS_DISTRIBUTION not in sent_types
    # In-scope members must be present
    assert CorporateActionsType.FORWARD_SPLIT in sent_types
    assert CorporateActionsType.CASH_DIVIDEND in sent_types
    assert CorporateActionsType.NAME_CHANGE in sent_types


def test_init_requires_corporate_actions_client_instance() -> None:
    """Non-``CorporateActionsClient`` argument raises ``TypeError`` at construction."""
    from alphamind.execution.broker_adapter.corporate_actions_queries import (
        CorporateActionsQueries,
    )

    with pytest.raises(TypeError):
        CorporateActionsQueries(object())  # type: ignore[arg-type]
