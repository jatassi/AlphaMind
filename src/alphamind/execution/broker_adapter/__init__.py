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

Order translation, fill-stream subscription, recovery, and venue constants
ship in subsequent stories per the dependency graph in the parent issue
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
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    SubmissionOutcome,
    Submitted,
    submit_with_retry,
)

__all__ = [
    "AlpacaClientFactory",
    "ExecutionMode",
    "GatewaySubmissionFailed",
    "PermanentRejection",
    "PermanentRejectionCode",
    "ResolvedCredentials",
    "SubmissionOutcome",
    "Submitted",
    "classify_alpaca_error",
    "is_transient",
    "submit_with_retry",
]
