"""Live underlying-price stream sub-package (story 02b / ALP-434).

Public surface:

* :class:`UnderlyingQuote` — frozen value object carrying ticker + price +
  tz-aware UTC ``as_of`` timestamp.
* :class:`UnderlyingPriceCache` — asyncio-safe in-memory writer / lock-free
  reader of latest quote per ticker; consumed by stories 03a / 03b / 04c.
* :data:`PriceRead` (:class:`FreshPrice` / :class:`StalePrice` /
  :class:`MissingPrice`, discriminated by :class:`PriceFreshness`) — the
  freshness-aware read outcome the enforcement-path consumers pattern-match on
  (ALP-827). :class:`GlobalStalenessSignal` is the cold-feed escalation signal
  the global-staleness detector returns.
* :func:`compute_target_underlyings` — pure projection over the repository's
  open-position set into the unique underlying-ticker subscription target.
* :func:`run_underlying_stream` — long-running asyncio task the monitor
  supervisor registers as ``underlying_stream``.
* :func:`register_underlying_stream_task` — entry-point wiring helper that
  constructs the cache and registers the task closure on a
  :class:`MonitorSupervisor`.
"""

from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    FreshPrice,
    GlobalStalenessSignal,
    MissingPrice,
    PriceFreshness,
    PriceRead,
    StalePrice,
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
    compute_target_underlyings,
)
from alphamind.execution.continuous_monitor.underlying_stream.task import (
    AlpacaStreamFactory,
    DefaultAlpacaStreamFactory,
    StockDataStreamProtocol,
    run_underlying_stream,
)
from alphamind.execution.continuous_monitor.underlying_stream.wiring import (
    register_underlying_stream_task,
)

__all__ = [
    "AlpacaStreamFactory",
    "DefaultAlpacaStreamFactory",
    "FreshPrice",
    "GlobalStalenessSignal",
    "MissingPrice",
    "OpenPositionsReader",
    "PriceFreshness",
    "PriceRead",
    "StalePrice",
    "StockDataStreamProtocol",
    "UnderlyingPriceCache",
    "UnderlyingQuote",
    "compute_target_underlyings",
    "register_underlying_stream_task",
    "run_underlying_stream",
]
