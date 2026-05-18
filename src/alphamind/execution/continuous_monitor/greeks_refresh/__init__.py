"""Greeks refresh sub-package (story 03a / ALP-436).

Maintains per-option-position freshness of ``OptionGreeks`` against the
continuous monitor's live underlying-price stream and the collector's
options-chains snapshots. The package exposes four building blocks:

* :class:`LastRefreshState` — frozen per-position anchor of the last refresh
  time and underlying price; consumed by the trigger predicate.
* :class:`IVQuote` and :func:`fetch_iv_from_options_chains` — typed
  SQLAlchemy reader against the collector-populated
  ``options_contract_snapshots`` table (parent issue ALP-123 decision (C)).
* :func:`recompute_greeks` — pure Black-Scholes call assembling a fresh
  :class:`OptionGreeks` value from spot + IV + risk-free rate + position
  geometry. Re-uses the guardrail-evaluation library's
  ``bs_greeks`` primitive — no new math layer.
* :func:`run_greeks_refresh` — long-running asyncio task the monitor
  supervisor registers as ``greeks_refresh``. Fires on whichever trigger
  comes first per
  ``docs/design/05-execution-layer/architecture.md`` § 4d:
  scheduled (``greeks_refresh_interval_minutes``) or move-based
  (``greeks_refresh_underlying_move_threshold_pct``).

Wave 3 of the continuous-monitor work tree (parent ALP-123). Downstream
consumers (breach evaluation, options bracket-stop firing) read greeks
through ``OptionGreeks`` fields on ``PositionRecord``; this task is the
single writer of those fields between invocations.
"""

from alphamind.execution.continuous_monitor.greeks_refresh.iv_provider import (
    IVQuote,
    fetch_iv_from_options_chains,
    fetch_iv_quotes_batch,
)
from alphamind.execution.continuous_monitor.greeks_refresh.recompute import (
    recompute_greeks,
    recompute_strategy_greeks,
)
from alphamind.execution.continuous_monitor.greeks_refresh.state import (
    LastRefreshState,
    seed_last_refresh_states,
)
from alphamind.execution.continuous_monitor.greeks_refresh.task import (
    GreeksWriter,
    run_greeks_refresh,
)
from alphamind.execution.continuous_monitor.greeks_refresh.wiring import (
    SqlGreeksWriter,
    register_greeks_refresh_task,
)

__all__ = [
    "GreeksWriter",
    "IVQuote",
    "LastRefreshState",
    "SqlGreeksWriter",
    "fetch_iv_from_options_chains",
    "fetch_iv_quotes_batch",
    "recompute_greeks",
    "recompute_strategy_greeks",
    "register_greeks_refresh_task",
    "run_greeks_refresh",
    "seed_last_refresh_states",
]
