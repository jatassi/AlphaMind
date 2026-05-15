"""Cross-cutting primitives for the AlphaMind data-sources library.

This package supersedes the legacy single-file ``data_sources/_common.py``
(ALP-473). Each concern now has a focused submodule:

- :mod:`alphamind.data_sources._common.config` — ``load_config``,
  ``AlphaMindConfig``.
- :mod:`alphamind.data_sources._common.retry` — ``RetryShape``,
  ``with_retries``.
- :mod:`alphamind.data_sources._common.rate_limit` — ``RateLimiter``.
- :mod:`alphamind.data_sources._common.run_tracking` — ``track_run``,
  ``RunState``, ``default_session_factory``.
- :mod:`alphamind.data_sources._common.resume` — ``resume_since``.
- :mod:`alphamind.data_sources._common.universe` —
  ``active_universe_tickers``.

The news-domain headline taxonomy moved out of ``_common`` entirely and now
lives at :mod:`alphamind.data_sources.news.types`. It is re-exported here
for backward compatibility with existing
``from alphamind.data_sources._common import HeadlineType`` callsites; new
code should import from ``news.types`` directly.

Curated re-exports below preserve every public symbol the 22 existing
importers depend on.
"""

from __future__ import annotations

from alphamind.data_sources._common.config import AlphaMindConfig, load_config
from alphamind.data_sources._common.rate_limit import RateLimiter
from alphamind.data_sources._common.resume import resume_since
from alphamind.data_sources._common.retry import RetryShape, with_retries
from alphamind.data_sources._common.run_tracking import (
    RunState,
    default_session_factory,
    track_run,
)
from alphamind.data_sources._common.universe import active_universe_tickers
from alphamind.data_sources.news.types import (
    HeadlineType,
    decode_topic_tags,
    normalize_vendor_tags,
)

__all__ = [
    "AlphaMindConfig",
    "HeadlineType",
    "RateLimiter",
    "RetryShape",
    "RunState",
    "active_universe_tickers",
    "decode_topic_tags",
    "default_session_factory",
    "load_config",
    "normalize_vendor_tags",
    "resume_since",
    "track_run",
    "with_retries",
]
