"""Order PATCH + DELETE translation (story 02e / ALP-383).

Translates canonical OMS commands that mutate or withdraw an existing order —
``AdjustCommand`` and ``CancelCommand`` — into Alpaca's
``replace_order_by_id(...)`` (cancel-and-replace) and
``cancel_order_by_id(...)`` REST calls, wrapped by ``submit_with_retry``.

Alpaca's PATCH-as-cancel-and-replace semantics: a PATCH returns a NEW Alpaca
order ID, the OMS's ``client_order_id`` migrates to the replacement, and the
OMS's ``alpaca_order_id_chain`` extends. See ``broker-adapter.md § Order
modification`` for the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import ReplaceOrderRequest

from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.errors import classify_alpaca_error
from alphamind.execution.broker_adapter.retry import (
    SubmissionOutcome,
    submit_with_retry,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplacementAck:
    """Alpaca's acknowledgment of a successful replace_order_by_id call.

    The replacement carries a NEW Alpaca order ID. The OMS appends it to
    the order's alpaca_order_id_chain and increments modification_count.
    The replaced original transitions to a 'replaced' terminal state via
    trade_updates (events handled by story 02f).
    """

    new_alpaca_order_id: str
    replaced_alpaca_order_id: str
    client_order_id: str
    status: str  # Alpaca's reported replacement status


@dataclass(frozen=True)
class CancellationAck:
    """Alpaca's acknowledgment of a fire-and-forget cancel.

    The terminal state (canceled, or race-condition filled) arrives via
    trade_updates separately.
    """

    alpaca_order_id: str
    accepted: bool


# ---------------------------------------------------------------------------
# Modifiable-field descriptor
# ---------------------------------------------------------------------------

AssetClass = Literal["us_equity", "us_option", "us_option_strategy"]
OrderClass = Literal["simple", "bracket", "oco", "oto", "mleg"]


@dataclass(frozen=True)
class ReplaceFields:
    """The intersection of OMS-modifiable fields and Alpaca-PATCHable fields."""

    limit_price: float | None = None
    stop_price: float | None = None
    qty: float | None = None
    trail_price: float | None = None  # equity-only
    trail_percent: float | None = None  # equity-only
    time_in_force: Literal["day", "gtc", "gtd"] | None = None


# ---------------------------------------------------------------------------
# Per-asset-class x order-class modifiable-field gates
#
# Each entry is a set of field names that ARE patchable for that combination.
# Any field not in the set raises ValueError before the SDK call.
# ---------------------------------------------------------------------------

# Per-surface tables — only the fields present in the set are allowed.
_EQUITY_SIMPLE_SURFACE: frozenset[str] = frozenset(
    {"limit_price", "stop_price", "qty", "trail_price", "trail_percent", "time_in_force"}
)
_EQUITY_BRACKET_SURFACE: frozenset[str] = frozenset({"limit_price", "stop_price"})
_EQUITY_OCO_SURFACE: frozenset[str] = frozenset({"limit_price", "stop_price"})
_EQUITY_OTO_SURFACE: frozenset[str] = frozenset(
    {"limit_price", "stop_price", "qty", "time_in_force"}
)
_OPTION_SIMPLE_SURFACE: frozenset[str] = frozenset(
    {"limit_price", "stop_price", "qty", "time_in_force"}
)
_MLEG_SURFACE: frozenset[str] = frozenset({"limit_price", "qty"})


def _resolve_surface(asset_class: AssetClass, order_class: OrderClass) -> frozenset[str]:
    """Return the set of PATCHable field names for this asset class x order class."""
    if asset_class == "us_option_strategy" and order_class == "mleg":
        return _MLEG_SURFACE
    if asset_class == "us_option":
        return _OPTION_SIMPLE_SURFACE
    # us_equity
    if order_class == "bracket":
        return _EQUITY_BRACKET_SURFACE
    if order_class == "oco":
        return _EQUITY_OCO_SURFACE
    if order_class == "oto":
        return _EQUITY_OTO_SURFACE
    # simple (or any other us_equity class)
    return _EQUITY_SIMPLE_SURFACE


def _requested_fields(fields: ReplaceFields) -> set[str]:
    """Return the names of fields in *fields* that are set (non-None)."""
    return {
        name
        for name in (
            "limit_price",
            "stop_price",
            "qty",
            "trail_price",
            "trail_percent",
            "time_in_force",
        )
        if getattr(fields, name) is not None
    }


def _validate_fields(
    fields: ReplaceFields,
    asset_class: AssetClass,
    order_class: OrderClass,
) -> None:
    """Raise ``ValueError`` if any requested field is out-of-surface.

    The error message names the offending field so the OMS handler can
    surface it back to the PM precisely.
    """
    surface = _resolve_surface(asset_class, order_class)
    requested = _requested_fields(fields)
    out_of_surface = requested - surface
    if out_of_surface:
        # Report the first offending field deterministically (sorted).
        bad = sorted(out_of_surface)[0]
        msg = (
            f"Field '{bad}' is not modifiable for asset_class={asset_class!r} "
            f"order_class={order_class!r}. "
            f"PATCHable fields: {sorted(surface)}"
        )
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# ReplaceOrderRequest builder
# ---------------------------------------------------------------------------

_TIF_MAP: dict[str, TimeInForce] = {
    "day": TimeInForce.DAY,
    "gtc": TimeInForce.GTC,
    # "gtd" is present in the OMS contract but not in the current alpaca-py
    # TimeInForce enum; it would surface as a validation failure at the
    # ReplaceOrderRequest level if Alpaca ever rejects it.
}


def _build_replace_request(fields: ReplaceFields) -> ReplaceOrderRequest:
    """Construct an alpaca-py ``ReplaceOrderRequest`` from *fields*.

    Only non-None fields are forwarded. ``trail_price`` / ``trail_percent``
    both map to alpaca-py's single ``trail`` parameter (only one should be set
    at a time — the surface table already limits this to simple equity orders).
    ``qty`` is cast to ``int`` as required by ``ReplaceOrderRequest``.
    """
    trail: float | None = (
        fields.trail_price if fields.trail_price is not None else fields.trail_percent
    )
    tif: TimeInForce | None = (
        _TIF_MAP.get(fields.time_in_force) if fields.time_in_force is not None else None
    )
    return ReplaceOrderRequest(
        qty=int(fields.qty) if fields.qty is not None else None,
        time_in_force=tif,
        limit_price=fields.limit_price,
        stop_price=fields.stop_price,
        trail=trail,
    )


# ---------------------------------------------------------------------------
# Shared retry classifier
# ---------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    """Return ``True`` if *exc* is a retriable transient error.

    Returning ``False`` for permanent 4xx rejections causes ``submit_with_retry``
    to re-raise the original ``APIError`` so the caller can call
    ``classify_alpaca_error`` and translate it to a synchronous OMS rejection.
    ``PermanentRejection`` is a frozen dataclass, not an Exception subclass, so
    raising it directly is not possible here.
    """
    return classify_alpaca_error(exc) is None


# ---------------------------------------------------------------------------
# Public submission API
# ---------------------------------------------------------------------------


async def submit_replace(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    target_alpaca_order_id: str,
    target_asset_class: AssetClass,
    target_order_class: OrderClass,
    fields: ReplaceFields,
) -> SubmissionOutcome[ReplacementAck]:
    """Validate fields against the per-asset-class modifiable surface, then
    call client.replace_order_by_id. Raises ``ValueError`` before any SDK
    call if a field is not modifiable for this asset class / order class.

    On success, returns ``Submitted[ReplacementAck]`` with the new Alpaca
    order ID (the replacement) and the original (replaced) order ID.

    On transient-error retry exhaustion, returns ``GatewaySubmissionFailed``.
    On permanent rejection (4xx), re-raises as ``PermanentRejection`` so the
    caller (engine-stub 03e) translates to a synchronous OMS rejection.
    """
    _validate_fields(fields, target_asset_class, target_order_class)
    replace_request = _build_replace_request(fields)

    async def _submit() -> ReplacementAck:
        # alpaca-py typing declares Union[Order, Dict]; cast to Order — the
        # dict-return path only occurs for internal SDK error responses, which
        # surface as APIError exceptions before the return value is used.
        response = cast(
            Order,
            client.replace_order_by_id(target_alpaca_order_id, order_data=replace_request),
        )
        return ReplacementAck(
            new_alpaca_order_id=str(response.id),
            replaced_alpaca_order_id=target_alpaca_order_id,
            client_order_id=str(response.client_order_id),
            status=str(response.status),
        )

    return await submit_with_retry(
        _submit,
        window_seconds=execution.submission_retry_window_seconds,
        transient_classifier=_is_transient,
    )


async def submit_cancel(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    target_alpaca_order_id: str,
) -> SubmissionOutcome[CancellationAck]:
    """Fire-and-forget cancel.

    Calls ``client.cancel_order_by_id(target_alpaca_order_id)`` and returns
    ``Submitted[CancellationAck(alpaca_order_id, accepted=True)]`` on success.
    The actual terminal state (canceled or race-condition filled) arrives via
    ``trade_updates`` separately.

    On transient-error retry exhaustion, returns ``GatewaySubmissionFailed``.
    On permanent rejection (404 not_found, 422 already-filled), re-raises as
    ``PermanentRejection``.
    """

    async def _submit() -> CancellationAck:
        client.cancel_order_by_id(target_alpaca_order_id)
        return CancellationAck(alpaca_order_id=target_alpaca_order_id, accepted=True)

    return await submit_with_retry(
        _submit,
        window_seconds=execution.submission_retry_window_seconds,
        transient_classifier=_is_transient,
    )
