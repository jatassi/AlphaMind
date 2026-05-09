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
Story 02c (ALP-381) — single-leg options POST translation + OCC builder.
Story 02d (ALP-382) — multi-leg mleg order POST translation.
Story 02e (ALP-383) — order PATCH + DELETE translation.
Story 02f (ALP-384) — fill-stream subscriber + translator.

Subsequent wave-3 stories ship in parallel; see the parent issue
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
from alphamind.execution.broker_adapter.order_mleg import (
    MLEGLegAck,
    MLEGSubmission,
    submit_mleg_add,
    submit_mleg_close,
    submit_mleg_open,
)
from alphamind.execution.broker_adapter.order_modify import (
    CancellationAck,
    ReplaceFields,
    ReplacementAck,
    submit_cancel,
    submit_replace,
)
from alphamind.execution.broker_adapter.order_options import (
    OptionsSubmission,
    build_occ_symbol,
    submit_options_add,
    submit_options_close,
    submit_options_open,
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
    "CancellationAck",
    "EquitySubmission",
    "ExecutionMode",
    "FillReport",
    "GatewaySubmissionFailed",
    "MLEGLegAck",
    "MLEGSubmission",
    "MarketClock",
    "OptionsSubmission",
    "OrderLegSnapshot",
    "OrderSnapshot",
    "OrderStatus",
    "PermanentRejection",
    "PermanentRejectionCode",
    "PositionSnapshot",
    "ReplaceFields",
    "ReplacementAck",
    "ResolvedCredentials",
    "SubmissionOutcome",
    "Submitted",
    "TradeAccountSnapshot",
    "build_occ_symbol",
    "classify_alpaca_error",
    "is_transient",
    "submit_cancel",
    "submit_equity_add",
    "submit_equity_close",
    "submit_equity_open",
    "submit_mleg_add",
    "submit_mleg_close",
    "submit_mleg_open",
    "submit_options_add",
    "submit_options_close",
    "submit_options_open",
    "submit_replace",
    "submit_with_retry",
    "subscribe_trade_updates",
    "translate_trade_update",
]
