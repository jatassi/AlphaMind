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

Story 02a (ALP-379) adds read-only account state wrappers:

* :class:`AccountStateQueries` — thin wrappers over the seven Alpaca REST
  GET endpoints the OMS, continuous monitor, paper-evaluation harness, and
  corporate-actions processor consume for state reconciliation.

Story 02f (ALP-384) adds the fill-stream subscriber + translator:

* :func:`subscribe_trade_updates`, :func:`translate_trade_update`,
  :class:`FillReport` — primitives the continuous monitor wraps with the
  run-forever lifecycle.

Order POST/PATCH translation, recovery, and venue constants ship in
subsequent stories per the dependency graph in the parent issue
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
    "submit_with_retry",
    "subscribe_trade_updates",
    "translate_trade_update",
]
