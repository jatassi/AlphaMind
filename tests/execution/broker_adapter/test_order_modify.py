"""Tests for ``alphamind.execution.broker_adapter.order_modify``.

Story ALP-383 — Order PATCH + DELETE translation.

Covers:
* Per-asset-class x order-class field-surface validation (positive + negative)
* Successful replace → Submitted[ReplacementAck] with correct ID chain
* Retry-success and retry-exhaustion paths
* Permanent-rejection re-raise (status-invalid PATCH, mleg leg-mutation 422)
* Simple cancel success → Submitted[CancellationAck]
* Cancel of unknown order (404) → PermanentRejection
* Cancel retry exhaustion → GatewaySubmissionFailed
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from alpaca.common.exceptions import APIError

from alphamind._kernel.ids import (
    AlpacaOrderId,
    ClientOrderId,
)
from alphamind.execution.broker_adapter import (
    GatewaySubmissionFailed,
    PermanentRejection,
    Submitted,
    classify_alpaca_error,
)
from alphamind.execution.broker_adapter.order_modify import (
    CancellationAck,
    ReplaceFields,
    ReplacementAck,
    submit_cancel,
    submit_replace,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_execution_config() -> Any:
    """Build a minimal ExecutionConfig-like object for tests."""
    from alphamind.config.models.execution import ExecutionConfig

    return ExecutionConfig.model_validate(
        {
            "greeks_refresh": {"scheduled_interval_minutes": 5, "move_trigger_pct": 0.02},
            "conservative_delta_buffer_pct": 0.05,
            "submission_retry_window_seconds": 5,
            "paper_harness": {
                "spread_buffer_pct": 0.001,
                "impact_coefficients": {
                    "market": 0.1,
                    "limit": 0.05,
                    "stop": 0.08,
                },
                "fee_schedule": {
                    "cat_per_executed_share": 0.0,
                    "taf_per_share_sells": 0.0,
                    "sec_pct_of_notional_sells": 0.0,
                    "orf_per_options_contract": 0.0,
                    "occ_per_options_contract": 0.0,
                },
            },
            "pl_target_margin_pct": 0.02,
        }
    )


def _make_api_error(status: int, message: str) -> APIError:
    """Build an ``APIError`` with the given status and message."""
    body = json.dumps({"code": status, "message": message})
    fake_http_error = MagicMock()
    fake_http_error.response.status_code = status
    return APIError(body, http_error=fake_http_error)  # type: ignore[no-untyped-call]


def _make_client() -> MagicMock:
    """Build a mock TradingClient."""
    return MagicMock()


def _mock_alpaca_order(
    order_id: str, client_order_id: str = "coid-1", status: str = "new"
) -> MagicMock:
    """Build a mock Alpaca Order response."""
    order = MagicMock()
    order.id = order_id
    order.client_order_id = client_order_id
    order.status = status
    return order


# ---------------------------------------------------------------------------
# ReplaceFields dataclass
# ---------------------------------------------------------------------------


def test_replace_fields_is_frozen_dataclass() -> None:
    fields = ReplaceFields(limit_price=100.0)
    with pytest.raises(FrozenInstanceError):
        fields.limit_price = 200.0  # type: ignore[misc]


def test_replace_fields_all_none_by_default() -> None:
    fields = ReplaceFields()
    assert fields.limit_price is None
    assert fields.stop_price is None
    assert fields.qty is None
    assert fields.trail_price is None
    assert fields.trail_percent is None
    assert fields.time_in_force is None


# ---------------------------------------------------------------------------
# Field-surface validation — us_equity / simple (widest surface)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields_kwargs",
    [
        {"limit_price": 100.0},
        {"stop_price": 95.0},
        {"qty": 10.0},
        {"trail_price": 5.0},
        {"trail_percent": 2.0},
        {"time_in_force": "day"},
        {"limit_price": 100.0, "stop_price": 95.0, "qty": 10.0, "time_in_force": "gtc"},
    ],
)
@pytest.mark.asyncio
async def test_submit_replace_us_equity_simple_accepts_all_fields(
    fields_kwargs: dict[str, Any],
) -> None:
    """us_equity / simple order accepts all six modifiable fields without ValueError."""
    client = _make_client()
    new_id = str(uuid4())
    client.replace_order_by_id.return_value = _mock_alpaca_order(new_id)
    execution = _make_execution_config()
    fields = ReplaceFields(**fields_kwargs)

    result = await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("orig-id-123"),
        target_asset_class="us_equity",
        target_order_class="simple",
        fields=fields,
    )

    # Should not raise ValueError — SDK was actually called
    assert isinstance(result, Submitted)
    assert isinstance(result.payload, ReplacementAck)


# ---------------------------------------------------------------------------
# Field-surface validation — us_option / simple rejects trail fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_field",
    ["trail_price", "trail_percent"],
)
@pytest.mark.asyncio
async def test_submit_replace_us_option_simple_rejects_trail_fields(
    bad_field: str,
) -> None:
    """us_option / simple order rejects trail_price and trail_percent."""
    client = _make_client()
    execution = _make_execution_config()
    fields = ReplaceFields(**{bad_field: 2.0})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=bad_field):
        await submit_replace(
            client=client,
            execution=execution,
            target_alpaca_order_id=AlpacaOrderId("orig-id-123"),
            target_asset_class="us_option",
            target_order_class="simple",
            fields=fields,
        )

    # SDK must not have been called
    client.replace_order_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_submit_replace_us_option_simple_accepts_valid_fields() -> None:
    """us_option / simple accepts limit_price, stop_price, qty, time_in_force."""
    client = _make_client()
    new_id = str(uuid4())
    client.replace_order_by_id.return_value = _mock_alpaca_order(new_id)
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=5.0, qty=2.0)

    result = await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("orig-id-123"),
        target_asset_class="us_option",
        target_order_class="simple",
        fields=fields,
    )

    assert isinstance(result, Submitted)
    assert isinstance(result.payload, ReplacementAck)


# ---------------------------------------------------------------------------
# Field-surface validation — mleg rejects everything except limit_price + qty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_field",
    ["stop_price", "trail_price", "trail_percent", "time_in_force"],
)
@pytest.mark.asyncio
async def test_submit_replace_mleg_rejects_non_mleg_fields(bad_field: str) -> None:
    """mleg order rejects all fields except limit_price and qty."""
    client = _make_client()
    execution = _make_execution_config()
    # Use a valid value for the field type
    value: Any = "day" if bad_field == "time_in_force" else 2.0
    fields = ReplaceFields(**{bad_field: value})

    with pytest.raises(ValueError, match=bad_field):
        await submit_replace(
            client=client,
            execution=execution,
            target_alpaca_order_id=AlpacaOrderId("orig-id-123"),
            target_asset_class="us_option_strategy",
            target_order_class="mleg",
            fields=fields,
        )

    client.replace_order_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_submit_replace_mleg_accepts_limit_price_and_qty() -> None:
    """mleg accepts limit_price and qty; constructs ReplaceOrderRequest with only those."""
    client = _make_client()
    new_id = str(uuid4())
    client.replace_order_by_id.return_value = _mock_alpaca_order(new_id)
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=850.0, qty=4.0)

    result = await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("orig-mleg-123"),
        target_asset_class="us_option_strategy",
        target_order_class="mleg",
        fields=fields,
    )

    assert isinstance(result, Submitted)
    # Verify the ReplaceOrderRequest only had limit_price and qty set
    call_args = client.replace_order_by_id.call_args
    replace_request = call_args[1].get("order_data") or call_args[0][1]
    assert replace_request.limit_price == 850.0
    assert replace_request.qty == 4
    assert replace_request.stop_price is None
    assert replace_request.trail is None
    assert replace_request.time_in_force is None


@pytest.mark.asyncio
async def test_submit_replace_mleg_with_only_limit_price_no_stop_in_request() -> None:
    """mleg with only limit_price: ReplaceOrderRequest has only limit_price set."""
    client = _make_client()
    new_id = str(uuid4())
    client.replace_order_by_id.return_value = _mock_alpaca_order(new_id)
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=850.0)

    await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("orig-mleg-123"),
        target_asset_class="us_option_strategy",
        target_order_class="mleg",
        fields=fields,
    )

    call_args = client.replace_order_by_id.call_args
    replace_request = call_args[1].get("order_data") or call_args[0][1]
    assert replace_request.limit_price == 850.0
    assert replace_request.qty is None
    assert replace_request.stop_price is None


# ---------------------------------------------------------------------------
# bracket / oco / oto child field restrictions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("order_class", "bad_field", "bad_value"),
    [
        # bracket child: only limit_price, stop_price
        ("bracket", "qty", 5.0),
        ("bracket", "trail_price", 1.0),
        ("bracket", "trail_percent", 1.0),
        ("bracket", "time_in_force", "day"),
        # oco child: only limit_price, stop_price
        ("oco", "qty", 5.0),
        ("oco", "trail_price", 1.0),
        ("oco", "time_in_force", "day"),
        # oto child: limit_price, stop_price, qty, time_in_force
        ("oto", "trail_price", 1.0),
        ("oto", "trail_percent", 1.0),
    ],
)
@pytest.mark.asyncio
async def test_submit_replace_equity_child_order_class_field_restrictions(
    order_class: str, bad_field: str, bad_value: Any
) -> None:
    """Equity bracket/oco/oto children have narrower modifiable surfaces."""
    client = _make_client()
    execution = _make_execution_config()
    fields = ReplaceFields(**{bad_field: bad_value})

    with pytest.raises(ValueError, match=bad_field):
        await submit_replace(
            client=client,
            execution=execution,
            target_alpaca_order_id=AlpacaOrderId("orig-id"),
            target_asset_class="us_equity",
            target_order_class=order_class,  # type: ignore[arg-type]
            fields=fields,
        )

    client.replace_order_by_id.assert_not_called()


# ---------------------------------------------------------------------------
# Successful replace → ReplacementAck ID chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_replace_success_returns_submitted_replacement_ack() -> None:
    """Successful Alpaca replace returns Submitted[ReplacementAck] with correct IDs."""
    client = _make_client()
    new_id = str(uuid4())
    orig_id = "orig-order-abc"
    client_order_id = "coid-xyz"
    client.replace_order_by_id.return_value = _mock_alpaca_order(
        new_id, client_order_id=client_order_id, status="pending_new"
    )
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=100.0)

    result = await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId(orig_id),
        target_asset_class="us_equity",
        target_order_class="simple",
        fields=fields,
    )

    assert isinstance(result, Submitted)
    ack = result.payload
    assert isinstance(ack, ReplacementAck)
    assert ack.new_alpaca_order_id == new_id
    assert ack.replaced_alpaca_order_id == orig_id
    assert ack.client_order_id == client_order_id
    assert ack.status == "pending_new"


# ---------------------------------------------------------------------------
# Retry exhaustion → GatewaySubmissionFailed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_replace_retry_exhaustion_returns_gateway_submission_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent transient errors past the window return GatewaySubmissionFailed."""
    import httpx

    client = _make_client()
    client.replace_order_by_id.side_effect = httpx.ConnectError("network down")
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=100.0)

    # Patch asyncio.sleep to be instant and time.monotonic to advance past the window quickly
    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    async def fake_sleep(seconds: float) -> None:
        fake_now[0] += seconds + 1.0  # advance past window quickly

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.time.monotonic", fake_monotonic)
    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    result = await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("orig-id"),
        target_asset_class="us_equity",
        target_order_class="simple",
        fields=fields,
    )

    assert isinstance(result, GatewaySubmissionFailed)
    assert result.last_error_class == "ConnectError"


# ---------------------------------------------------------------------------
# Permanent rejection re-raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_replace_permanent_rejection_reraises() -> None:
    """422 status-invalid PATCH re-raises (via submit_with_retry) as a permanent error.

    ``PermanentRejection`` is a frozen dataclass, not an Exception subclass —
    the underlying ``APIError`` is re-raised by ``submit_with_retry`` when the
    transient classifier returns ``False``.  The caller (03e) then calls
    ``classify_alpaca_error`` to translate to a ``PermanentRejection`` value.
    """
    from alpaca.common.exceptions import APIError

    client = _make_client()
    client.replace_order_by_id.side_effect = _make_api_error(
        422, "order is in pending_new status, cannot replace"
    )
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=100.0)

    with pytest.raises(APIError) as exc_info:
        await submit_replace(
            client=client,
            execution=execution,
            target_alpaca_order_id=AlpacaOrderId("orig-id"),
            target_asset_class="us_equity",
            target_order_class="simple",
            fields=fields,
        )

    # Verify that classify_alpaca_error produces a PermanentRejection for it
    rejection = classify_alpaca_error(exc_info.value)
    assert rejection is not None
    assert isinstance(rejection, PermanentRejection)
    assert rejection.http_status == 422


@pytest.mark.asyncio
async def test_submit_replace_mleg_leg_mutation_422_reraises() -> None:
    """mleg leg-structure 422 surfaces via APIError with PermanentRejection classification."""
    from alpaca.common.exceptions import APIError

    client = _make_client()
    client.replace_order_by_id.side_effect = _make_api_error(
        422, "invalid legs[0]: cannot modify leg structure"
    )
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=850.0)

    with pytest.raises(APIError) as exc_info:
        await submit_replace(
            client=client,
            execution=execution,
            target_alpaca_order_id=AlpacaOrderId("orig-mleg"),
            target_asset_class="us_option_strategy",
            target_order_class="mleg",
            fields=fields,
        )

    rejection = classify_alpaca_error(exc_info.value)
    assert rejection is not None
    assert isinstance(rejection, PermanentRejection)
    assert rejection.http_status == 422
    assert rejection.code == "invalid_legs"


# ---------------------------------------------------------------------------
# submit_cancel — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_cancel_success_returns_submitted_cancellation_ack() -> None:
    """Successful cancel returns Submitted[CancellationAck(accepted=True)]."""
    client = _make_client()
    client.cancel_order_by_id.return_value = None  # fire-and-forget, returns None
    execution = _make_execution_config()
    target_id = "alpaca-order-to-cancel"

    result = await submit_cancel(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId(target_id),
    )

    assert isinstance(result, Submitted)
    ack = result.payload
    assert isinstance(ack, CancellationAck)
    assert ack.alpaca_order_id == target_id
    assert ack.accepted is True
    client.cancel_order_by_id.assert_called_once_with(target_id)


# ---------------------------------------------------------------------------
# submit_cancel — unknown order → PermanentRejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_cancel_unknown_order_raises_permanent_rejection() -> None:
    """Cancel of an unknown / already-canceled order (404) re-raises as APIError.

    ``PermanentRejection`` is a frozen dataclass, not an Exception — the
    underlying ``APIError`` propagates; the caller uses ``classify_alpaca_error``
    to produce the ``PermanentRejection`` value.
    """
    from alpaca.common.exceptions import APIError

    client = _make_client()
    client.cancel_order_by_id.side_effect = _make_api_error(404, "order not found")
    execution = _make_execution_config()

    with pytest.raises(APIError) as exc_info:
        await submit_cancel(
            client=client,
            execution=execution,
            target_alpaca_order_id=AlpacaOrderId("ghost-order-id"),
        )

    rejection = classify_alpaca_error(exc_info.value)
    assert rejection is not None
    assert isinstance(rejection, PermanentRejection)
    assert rejection.http_status == 404


@pytest.mark.asyncio
async def test_submit_cancel_already_filled_order_raises_permanent_rejection() -> None:
    """Cancel of an already-filled order (422) re-raises as APIError (permanent)."""
    from alpaca.common.exceptions import APIError

    client = _make_client()
    client.cancel_order_by_id.side_effect = _make_api_error(422, "cannot cancel a filled order")
    execution = _make_execution_config()

    with pytest.raises(APIError) as exc_info:
        await submit_cancel(
            client=client,
            execution=execution,
            target_alpaca_order_id=AlpacaOrderId("filled-order-id"),
        )

    rejection = classify_alpaca_error(exc_info.value)
    assert rejection is not None
    assert isinstance(rejection, PermanentRejection)
    assert rejection.http_status == 422


# ---------------------------------------------------------------------------
# submit_cancel — retry exhaustion → GatewaySubmissionFailed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_cancel_retry_exhaustion_returns_gateway_submission_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent network failure on cancel returns GatewaySubmissionFailed."""
    import httpx

    client = _make_client()
    client.cancel_order_by_id.side_effect = httpx.ConnectError("connection refused")
    execution = _make_execution_config()

    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    async def fake_sleep(seconds: float) -> None:
        fake_now[0] += seconds + 1.0

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.time.monotonic", fake_monotonic)
    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    result = await submit_cancel(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("target-order-id"),
    )

    assert isinstance(result, GatewaySubmissionFailed)
    assert result.last_error_class == "ConnectError"


# ---------------------------------------------------------------------------
# Sync SDK calls run on the worker thread pool — both replace and cancel
# (regression: alpaca-py's REST methods are sync; calling them inline on the
# event loop blocks every other coroutine for the duration of the HTTP RTT).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_replace_runs_sdk_call_on_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``client.replace_order_by_id`` runs via ``asyncio.to_thread``.

    Tracks ``threading.current_thread()`` inside the SDK callable and asserts
    it is NOT the main thread the test runs on. Mirrors the equity / options /
    mleg sibling pattern; the regression we guard against is calling the sync
    SDK directly from the coroutine.
    """
    import threading

    main_thread = threading.current_thread()
    observed: dict[str, threading.Thread] = {}

    def _capturing_replace(*args: Any, **kwargs: Any) -> Any:
        observed["thread"] = threading.current_thread()
        return _mock_alpaca_order(str(uuid4()))

    client = _make_client()
    client.replace_order_by_id.side_effect = _capturing_replace
    execution = _make_execution_config()
    fields = ReplaceFields(limit_price=100.0)

    result = await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("orig-id"),
        target_asset_class="us_equity",
        target_order_class="simple",
        fields=fields,
    )

    assert isinstance(result, Submitted)
    assert "thread" in observed
    assert observed["thread"] is not main_thread, (
        f"replace_order_by_id ran on the main thread {main_thread.name!r}; "
        f"expected a worker-pool thread (asyncio.to_thread offload)."
    )


@pytest.mark.asyncio
async def test_submit_cancel_runs_sdk_call_on_worker_thread() -> None:
    """``client.cancel_order_by_id`` runs via ``asyncio.to_thread``.

    Same regression-guard as the replace counterpart.
    """
    import threading

    main_thread = threading.current_thread()
    observed: dict[str, threading.Thread] = {}

    def _capturing_cancel(*args: Any, **kwargs: Any) -> None:
        observed["thread"] = threading.current_thread()

    client = _make_client()
    client.cancel_order_by_id.side_effect = _capturing_cancel
    execution = _make_execution_config()

    result = await submit_cancel(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId("some-id"),
    )

    assert isinstance(result, Submitted)
    assert "thread" in observed
    assert observed["thread"] is not main_thread, (
        f"cancel_order_by_id ran on the main thread {main_thread.name!r}; "
        f"expected a worker-pool thread (asyncio.to_thread offload)."
    )


# ---------------------------------------------------------------------------
# Cancellation does not require client_order_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_cancel_uses_alpaca_order_id_directly() -> None:
    """Cancel uses target Alpaca order ID — no client_order_id needed."""
    client = _make_client()
    client.cancel_order_by_id.return_value = None
    execution = _make_execution_config()
    target_id = "alpaca-direct-id-789"

    await submit_cancel(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId(target_id),
    )

    # The mock should have been called with the Alpaca order ID directly
    client.cancel_order_by_id.assert_called_once_with(target_id)


# ---------------------------------------------------------------------------
# ReplacementAck / CancellationAck frozen dataclasses
# ---------------------------------------------------------------------------


def test_replacement_ack_is_frozen() -> None:
    ack = ReplacementAck(
        new_alpaca_order_id=AlpacaOrderId("new"),
        replaced_alpaca_order_id=AlpacaOrderId("old"),
        client_order_id=ClientOrderId("coid"),
        status="pending_new",
    )
    with pytest.raises(FrozenInstanceError):
        ack.new_alpaca_order_id = AlpacaOrderId("mutated")  # type: ignore[misc]


def test_cancellation_ack_is_frozen() -> None:
    ack = CancellationAck(alpaca_order_id=AlpacaOrderId("abc"), accepted=True)
    with pytest.raises(FrozenInstanceError):
        ack.accepted = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Public surface importable from broker_adapter package
# ---------------------------------------------------------------------------


def test_public_surface_importable_from_package() -> None:
    """All new symbols importable from alphamind.execution.broker_adapter."""
    from alphamind.execution.broker_adapter import (  # noqa: F401
        CancellationAck,
        ReplaceFields,
        ReplacementAck,
        submit_cancel,
        submit_replace,
    )
