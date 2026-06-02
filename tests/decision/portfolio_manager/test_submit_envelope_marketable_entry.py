"""Integration: quote_source threads enter-now entries to marketable limits — ALP-738.

Proves the wiring end-to-end through ``_handle_submit_envelope``: when a live
quote source is supplied, an enter-now equity OPEN (a limit with no analyst
entry_window) is re-priced to a marketable limit *before* broker dispatch, so the
command the broker receives carries the marketable price — not the analyst's
static away-from-market limit. The companion unit tests in
``tests/execution/broker_adapter/test_entry_pricing.py`` cover the pricing math;
this test catches a regression where the ``quote_source`` kwarg is dropped
between ``build_submit_envelope_mcp_server`` and the rewrite call.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId
from alphamind._kernel.money import price
from alphamind.commands.command_models import EntryOrder, OMSCommand, OpenCommand
from alphamind.execution.broker_adapter.entry_pricing import TouchQuote
from tests.execution.oms.test_engine_stub_broker_routing import _default_execution_config
from tests.execution.oms.test_submit_envelope_mcp import (
    _DEFAULT_ACTIVE_SECTORS,
    _make_analyst_envelope,
    _make_bundle,
    _make_pm_view,
    _make_validation_state,
    _open_command,
    _recommendation_stub,
    _retrieval_store,
    _sector_resolver,
)


class _FakeQuoteSource:
    def __init__(self, quotes: dict[str, TouchQuote | None]) -> None:
        self._quotes = quotes

    async def latest_quote(self, symbol: str) -> TouchQuote | None:
        return self._quotes.get(symbol)


class _CapturingBrokerDispatch:
    """Captures each dispatched command and returns a Submitted ack."""

    def __init__(self) -> None:
        self.captured: list[OMSCommand] = []

    async def __call__(self, command: OMSCommand, *, client_order_id: str, **context: Any) -> Any:
        from alphamind.execution.broker_adapter import EquitySubmission, Submitted
        from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult

        self.captured.append(command)
        oid = AlpacaOrderId(str(uuid.uuid4()))
        return Submitted(
            payload=BrokerDispatchResult(
                alpaca_order_id=oid,
                client_order_id=ClientOrderId(client_order_id),
                status="accepted",
                order_class="simple",
                payload_kind="equity",
                raw_submission=EquitySubmission(
                    alpaca_order_id=oid,
                    client_order_id=ClientOrderId(client_order_id),
                    status="accepted",
                    order_class="simple",
                ),
            ),
            attempt_count=1,
        )


def _enter_now_limit_envelope() -> Any:
    """Analyst envelope whose single OPEN is an enter-now limit (no entry_window)."""
    enter_now = _open_command().model_copy(
        update={"entry_order": EntryOrder(type="limit", limit_price=price("950.0"))}
    )
    return _make_analyst_envelope(commands=(enter_now,))


async def _dispatch(envelope: Any, *, quote_source: Any) -> _CapturingBrokerDispatch:
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.broker_adapter import AccountStateQueries

    validation_state = _make_validation_state()
    state = build_initial_submit_envelope_state(
        invocation_id=validation_state.invocation_id,
        starting_validation_state=validation_state,
    )
    fake_dispatch = _CapturingBrokerDispatch()
    _response, _state = await _handle_submit_envelope(
        envelope.model_dump(mode="json"),
        state=state,
        retrieval_store=_retrieval_store(),
        pre_processor_bundle=_make_bundle(recommendations=(_recommendation_stub("REC-1"),)),
        pm_view=_make_pm_view(),
        active_sectors=_DEFAULT_ACTIVE_SECTORS,
        halt_mode=False,
        sector_resolver=_sector_resolver,
        state_persistence_config=MagicMock(),
        invocation_handle=None,  # no DB writeback (Step 6 gated off)
        client=MagicMock(),
        queries=MagicMock(spec=AccountStateQueries),
        execution_config=_default_execution_config(),
        quote_source=quote_source,
        broker_dispatch=fake_dispatch,
    )
    return fake_dispatch


@pytest.mark.asyncio
async def test_quote_source_repricing_reaches_the_broker() -> None:
    """With a quote source, the dispatched command carries the marketable limit."""
    # _open_command() is a coherent long bracket (stop 750 below, target 950
    # above); the quote sits in that band so the marketable limit stays coherent.
    quote_source = _FakeQuoteSource({"NVDA": TouchQuote(bid=price("899.98"), ask=price("900.00"))})

    fake_dispatch = await _dispatch(_enter_now_limit_envelope(), quote_source=quote_source)

    (command,) = fake_dispatch.captured
    assert isinstance(command, OpenCommand)
    # long enter-now -> marketable BUY limit through the ask:
    # 900.00 * (1 + 0.0005) = 900.45 (default bps=5.0), not the analyst's 950.
    assert command.entry_order.limit_price == price("900.45")


@pytest.mark.asyncio
async def test_no_quote_source_leaves_entry_verbatim() -> None:
    """Without a quote source the analyst's static limit is dispatched unchanged."""
    fake_dispatch = await _dispatch(_enter_now_limit_envelope(), quote_source=None)

    (command,) = fake_dispatch.captured
    assert isinstance(command, OpenCommand)
    assert command.entry_order.limit_price == price("950.0")


@pytest.mark.asyncio
async def test_repriced_envelope_carries_reprice_marker_in_submission_log() -> None:
    """ALP-765: a repriced enter-now entry stamps reprice_markers on the log entry.

    The submission_log entry for a repriced envelope must carry a reprice marker
    with the ticker, analyst price, and marketable price so the pm_decision audit
    row records the execution-layer price movement — the envelope can no longer
    read as 'approve / no modifications' when the price was silently moved.
    """
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.broker_adapter import AccountStateQueries

    quote_source = _FakeQuoteSource({"NVDA": TouchQuote(bid=price("899.98"), ask=price("900.00"))})
    validation_state = _make_validation_state()
    state = build_initial_submit_envelope_state(
        invocation_id=validation_state.invocation_id,
        starting_validation_state=validation_state,
    )
    envelope = _enter_now_limit_envelope()  # analyst limit_price = 950.0
    fake_dispatch = _CapturingBrokerDispatch()

    _response, new_state = await _handle_submit_envelope(
        envelope.model_dump(mode="json"),
        state=state,
        retrieval_store=_retrieval_store(),
        pre_processor_bundle=_make_bundle(recommendations=(_recommendation_stub("REC-1"),)),
        pm_view=_make_pm_view(),
        active_sectors=_DEFAULT_ACTIVE_SECTORS,
        halt_mode=False,
        sector_resolver=_sector_resolver,
        state_persistence_config=MagicMock(),
        invocation_handle=None,
        client=MagicMock(),
        queries=MagicMock(spec=AccountStateQueries),
        execution_config=_default_execution_config(),
        quote_source=quote_source,
        broker_dispatch=fake_dispatch,
    )

    assert len(new_state.submission_log) == 1
    log_entry = new_state.submission_log[0]
    assert len(log_entry.reprice_markers) == 1
    marker = log_entry.reprice_markers[0]
    assert marker["ticker"] == "NVDA"
    assert marker["analyst_price"] == "950.0"
    # long enter-now: 900.00 x (1 + 0.0005) = 900.45
    assert marker["marketable_price"] == "900.45"
