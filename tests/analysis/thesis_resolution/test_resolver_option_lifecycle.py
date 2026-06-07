"""End-to-end resolver tests for option-lifecycle-closed theses — ALP-918.

The closed-position thesis resolver (04e) reads the exit method off a
``POSITION_CLOSED`` activity-log entry and realized P/L off ``thesis_pnl_ledger``.
ALP-918 makes the option-close path emit that entry (and re-point an
assignment/exercise thesis to the delivered equity leg), so these tests drive
the *real* option-lifecycle handler, re-derive the ledger, then run the
resolver — confirming an expiry-closed option thesis resolves ``ACTIVE →
RESOLVED`` and an assignment thesis defers to the equity close.

Distinct from ``test_resolver.py`` (the 04e resolver unit tests) and the 04h
hardening tests: this file owns the option-lifecycle-close integration path.

Mocks only the SDK (the LLM-fallback path the ENTRY_RATIONALE component takes)
and the database (the on-disk session from the shared ``db`` fixture).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.analysis.thesis_resolution.resolver import resolve_closed_position_theses
from alphamind.execution.account_activities.dispatch import integrate_lifecycle_event
from alphamind.execution.account_activities.records import (
    LifecycleActivityType,
    LifecycleEvent,
    TradeLeg,
)
from alphamind.execution.write_paths.thesis_pnl_ledger import rederive_thesis_pnl_ledger
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.theses import (
    ThesisRecordStatus,
    ThesisResolutionCategory,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.theses import ThesisRow
from tests.analysis.thesis_resolution.conftest import make_evaluator_config
from tests.execution.corporate_actions._handler_substrate import (
    INV_ID,
    make_active_bracket,
    make_active_thesis,
    make_open_options_position,
    make_pending_entry_order,
    open_handle,
    seed_invocation_substrate,
    seed_position_cluster,
)

pytestmark = pytest.mark.asyncio

_TXN = dt.datetime(2026, 9, 18, 20, 0, 0, tzinfo=dt.UTC)
_OCC = "AAPL260918C00150000"


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _sdk_response(outcome: str = "WRONG") -> list[Any]:
    from claude_agent_sdk import AssistantMessage, ResultMessage

    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assistant = AssistantMessage(
        content=[], model="claude-sonnet-4-6", stop_reason="end_turn", usage=usage
    )
    result = ResultMessage(
        subtype="result",
        duration_ms=500,
        duration_api_ms=450,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason="end_turn",
        usage=usage,
        structured_output={"outcome": outcome, "notes": "Qualitative read."},
    )
    return [assistant, result]


def _make_sdk_stub(outcome: str = "WRONG") -> Any:
    async def _stub(**_kwargs: Any) -> AsyncIterator[Any]:
        async for msg in _async_iter(_sdk_response(outcome)):
            yield msg

    return _stub


def _no_sdk(**_kwargs: Any) -> Any:
    raise AssertionError("the deferred-assignment path must not assess the thesis")


async def _seed_open_option(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_invocation_substrate(factory)
    position = make_open_options_position(
        position_id="pos-1",
        thesis_id="thesis-1",
        bracket_id="brk-1",
        contract_count=5.0,
        premium_paid_per_contract=250.0,
    )
    await seed_position_cluster(
        factory,
        position=position,
        order=make_pending_entry_order(),
        thesis=make_active_thesis(thesis_id="thesis-1", position_id="pos-1"),
        bracket=make_active_bracket(bracket_id="brk-1", position_id="pos-1"),
    )


def _borrow_resolver(_ticker: str) -> float | None:
    return 12.0


def _expiry_event() -> LifecycleEvent:
    return LifecycleEvent(
        activity_id="act-exp-1",
        activity_type=LifecycleActivityType.OPEXP,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=None,
    )


def _assignment_event() -> LifecycleEvent:
    return LifecycleEvent(
        activity_id="act-asn-1",
        activity_type=LifecycleActivityType.OPASN,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=TradeLeg(
            activity_id="act-trd-1",
            equity_symbol="AAPL",
            qty=500.0,
            strike_price=price(150.0),
            side="buy",
            net_amount=signed_money(-75_000.0),
        ),
    )


async def _run_handler(
    factory: async_sessionmaker[AsyncSession], event: LifecycleEvent
) -> None:
    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(handle, event, borrow_cost_resolver=_borrow_resolver)
    finally:
        await ctx.__aexit__(None, None, None)


async def _rederive_ledger(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        await rederive_thesis_pnl_ledger(sess, ThesisId("thesis-1"), None)
        await sess.commit()


async def test_expiry_closed_option_thesis_resolves_active_to_resolved(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC5: an OTM-expiry-closed option whose linked thesis is ACTIVE is resolved
    ``ACTIVE → RESOLVED`` by ``resolve_closed_position_theses`` — the resolver
    finds the POSITION_CLOSED entry the expiry handler emitted (not skipped, not
    raised). -premium P/L + OPTION_EXPIRY mechanical exit → stopped-correctly."""
    _engine, factory = db
    await _seed_open_option(factory)
    await _run_handler(factory, _expiry_event())
    await _rederive_ledger(factory)

    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=INV_ID)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=_make_sdk_stub("WRONG"),
        )
        await session.commit()

    assert len(resolved) == 1
    assert resolved[0].record.resolution_pnl_usd == pytest.approx(-1250.0)

    async with factory() as session:
        thesis_row = await session.get(ThesisRow, "thesis-1")
        assert thesis_row is not None
        assert thesis_row.status == ThesisRecordStatus.RESOLVED.value
        assert (
            thesis_row.resolution_category
            == ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY.value
        )


async def test_assignment_thesis_defers_to_equity_close_then_resolves(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC7: after an assignment, the resolver leaves the thesis ACTIVE (it points
    at the still-OPEN delivered equity, not the closed option). After the equity
    position subsequently closes (an equity POSITION_CLOSED + sell fill), the
    resolver resolves ``ACTIVE → RESOLVED`` reading the equity's exit method and
    a resolution P/L equal to the full ledger P/L (option -premium + equity
    realized)."""
    _engine, factory = db
    await _seed_open_option(factory)
    await _run_handler(factory, _assignment_event())
    await _rederive_ledger(factory)

    equity_position_id = "pos-eq-act-asn-1"

    # 1) Before the equity closes: the resolver finds no CLOSED position for the
    #    re-pointed thesis, so it resolves nothing — the thesis stays ACTIVE.
    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=INV_ID)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=_no_sdk,
        )
        await session.commit()
    assert resolved == ()
    async with factory() as session:
        thesis_row = await session.get(ThesisRow, "thesis-1")
        assert thesis_row is not None
        assert thesis_row.status == ThesisRecordStatus.ACTIVE.value
        assert thesis_row.position_id == equity_position_id

    # 2) Close the delivered equity leg: a sell FILL realizing +5000 on the
    #    long 500 @ 150 (sell @ 160), the equity POSITION_CLOSED carrying the
    #    real equity exit method (the path fill_collection drives), and the
    #    CLOSED status — then re-derive the ledger.
    close_at = _TXN + dt.timedelta(hours=1)
    async with factory() as session:
        session.add(
            BrokerEventLogRow(
                event_key="fill:sell-1",
                event_type="FILL",
                thesis_id="thesis-1",
                invocation_id=INV_ID,
                position_id=equity_position_id,
                raw_payload_json=(
                    '{"fill_price": 160.0, "fill_quantity": 500.0, '
                    '"raw_event_payload": {"order": {"side": "sell"}}}'
                ),
                broker_timestamp=close_at,
                captured_at=close_at,
            )
        )
        equity_row = await session.get(PositionRow, equity_position_id)
        assert equity_row is not None
        equity_row.status = "CLOSED"
        equity_close_entry = ActivityLogEntry(
            entry_id=f"{INV_ID}-POSITION_CLOSED-{uuid.uuid4().hex}",
            invocation_id=INV_ID,
            timestamp=close_at,
            event_type=EventType.POSITION_CLOSED,
            event_group=EVENT_TYPE_TO_GROUP[EventType.POSITION_CLOSED],
            position_id=equity_position_id,
            order_id=None,
            thesis_id="thesis-1",
            source=EventSource.FILL_PROCESSOR,
            detail=PositionClosedDetail(
                exit_method=PositionExitMethod.TARGET_REACHED,
                exit_price=money(160.0),
                realized_pnl_usd=signed_money(5000.0),
                thesis_resolution_category="",
            ),
        )
        session.add(activity_log_entry_to_row(equity_close_entry))
        await session.commit()

    await _rederive_ledger(factory)

    # 3) Now the resolver resolves the thesis off the equity close: full ledger
    #    P/L = -1250 (option premium) + 5000 (equity realized) = +3750.
    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=INV_ID)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=_make_sdk_stub("VALIDATED"),
        )
        await session.commit()

    assert len(resolved) == 1
    assert resolved[0].record.resolution_pnl_usd == pytest.approx(3750.0)
    async with factory() as session:
        thesis_row = await session.get(ThesisRow, "thesis-1")
        assert thesis_row is not None
        assert thesis_row.status == ThesisRecordStatus.RESOLVED.value
