"""Broker adapter — thin Alpaca surface for the OMS (ALP-121).

Story 01 (ALP-378) ships the foundational substrate:

* :class:`AlpacaClientFactory` — resolves paper/live credentials from
  :class:`alphamind.config.models.venue.VenueConfig` + environment variables
  and mints fresh ``alpaca-py`` REST / websocket clients.
* :func:`submit_with_retry` — wraps a coroutine with bounded
  exponential-backoff retry; returns typed :class:`Submitted` /
  :class:`GatewaySubmissionFailed` outcomes.
* :func:`classify_alpaca_error` — maps ``alpaca-py`` ``APIError`` plus
  network/timeout exceptions into the broker adapter's typed
  :class:`PermanentRejection` taxonomy aligned with
  ``broker-adapter.md § Order submission``.

Story 02a (ALP-379) — :class:`AccountStateQueries` wrappers.
Story 02b (ALP-380) — equity order POST translation.
Story 02f (ALP-384) — fill-stream subscriber + translator.

Subsequent wave-2 stories ship in parallel; see the parent issue
(`ALP-121 <https://linear.app/alphamind-jatassi/issue/ALP-121>`_).
"""

from alphamind.execution.broker_adapter.client_factory import (
    AlpacaClientFactory,
    ExecutionMode,
    ResolvedCredentials,
)
from alphamind.execution.broker_adapter.errors import (
    PermanentRejection,
    PermanentRejectionCode,
    classify_alpaca_error,
    is_transient,
)
from alphamind.execution.broker_adapter.fill_stream import (
    FillReport,
    OrderStatus,
    subscribe_trade_updates,
    translate_trade_update,
)
from alphamind.execution.broker_adapter.order_equity import (
    EquitySubmission,
    submit_equity_add,
    submit_equity_close,
    submit_equity_open,
)
from alphamind.execution.broker_adapter.queries import (
    AccountStateQueries,
    ActivitySnapshot,
    AssetSnapshot,
    CalendarDay,
    MarketClock,
    OrderLegSnapshot,
    OrderSnapshot,
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    SubmissionOutcome,
    Submitted,
    submit_with_retry,
)

__all__ = [
    "AccountStateQueries",
    "ActivitySnapshot",
    "AlpacaClientFactory",
    "AssetSnapshot",
    "CalendarDay",
    "EquitySubmission",
    "ExecutionMode",
    "FillReport",
    "GatewaySubmissionFailed",
    "MarketClock",
    "OrderLegSnapshot",
    "OrderSnapshot",
    "OrderStatus",
    "PermanentRejection",
    "PermanentRejectionCode",
    "PositionSnapshot",
    "ResolvedCredentials",
    "SubmissionOutcome",
    "Submitted",
    "TradeAccountSnapshot",
    "classify_alpaca_error",
    "is_transient",
    "submit_equity_add",
    "submit_equity_close",
    "submit_equity_open",
    "submit_with_retry",
    "subscribe_trade_updates",
    "translate_trade_update",
]
