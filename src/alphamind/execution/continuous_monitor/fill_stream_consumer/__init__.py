"""Fill-stream consumer task package (story 02c — ALP-435).

Wraps the broker-adapter's :func:`subscribe_trade_updates` primitive in a
long-running asyncio task that translates each yielded :class:`FillReport`
into a persistence-layer :class:`FillRecord` and appends it via
:func:`append_fill_record`. On websocket disconnect the task invokes
:func:`recover_missed_fills_since` to catch up missed events through
Alpaca's REST ``GET /v2/orders`` surface before resuming the websocket loop.

Public surface re-exported here mirrors what the story acceptance criteria
pin: the pure translator :func:`fill_report_to_fill_record` and the
run-forever :func:`run_fill_stream_consumer` task.
"""

from alphamind.execution.continuous_monitor.fill_stream_consumer.task import (
    AccountStateQueriesFactory,
    EnrichmentCallable,
    TradingClientFactory,
    TradingStreamFactory,
    run_fill_stream_consumer,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    fill_report_to_fill_record,
)

__all__ = [
    "AccountStateQueriesFactory",
    "EnrichmentCallable",
    "TradingClientFactory",
    "TradingStreamFactory",
    "fill_report_to_fill_record",
    "run_fill_stream_consumer",
]
