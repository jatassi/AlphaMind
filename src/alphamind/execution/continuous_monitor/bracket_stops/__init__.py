"""Options bracket-stop firing sub-package (story 04c / ALP-440).

Per ``docs/design/05-execution-layer/architecture.md`` § 4e and the parent
issue ALP-123 decision (I), the continuous monitor owns options bracket-stop
firing: a per-position watcher evaluates price-based invalidation triggers
against the underlying-price cache (story 02b) and P/L-based target / stop
triggers against derived option prices (using the freshly-refreshed greeks
from story 03a + the guardrail-evaluation Black-Scholes core). On trigger
fire, the closer submits a closing market order via the broker adapter
directly — NOT wrapped as an engine envelope — and persists a
``POSITION_CLOSED`` activity-log entry with
``event_source=BRACKET_MANAGER``.

The package exposes five building blocks:

* :func:`evaluate_price_based_trigger` and :func:`evaluate_pl_target_trigger`
  — pure, deterministic trigger evaluators.
* :class:`BracketCloseSubmitter` — Protocol abstracting the direct
  broker-adapter call surface. Production wires it to
  ``broker_adapter.order_options.submit_options_close`` /
  ``order_mleg.submit_mleg_close``; tests substitute a capturing fake.
* :func:`submit_options_bracket_close` — orchestrator that selects the
  right close path for a position, calls the submitter, and persists the
  ``POSITION_CLOSED`` activity-log entry.
* :func:`run_options_bracket_watcher` — long-running task the supervisor
  registers as ``bracket_stops``. Tracks per-bracket-leg already-fired
  state so each leg fires at most once per session.
* :func:`register_options_bracket_watcher_task` — supervisor wiring helper.
"""

from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    BracketCloseSubmitter,
    prepare_bracket_close,
    submit_options_bracket_close,
)
from alphamind.execution.continuous_monitor.bracket_stops.task import (
    run_options_bracket_watcher,
)
from alphamind.execution.continuous_monitor.bracket_stops.triggers import (
    evaluate_pl_target_trigger,
    evaluate_price_based_trigger,
)
from alphamind.execution.continuous_monitor.bracket_stops.wiring import (
    AlpacaBracketCloseSubmitter,
    SqlBracketRepository,
    register_options_bracket_watcher_task,
)

__all__ = [
    "AlpacaBracketCloseSubmitter",
    "BracketCloseSubmitter",
    "SqlBracketRepository",
    "evaluate_pl_target_trigger",
    "evaluate_price_based_trigger",
    "prepare_bracket_close",
    "register_options_bracket_watcher_task",
    "run_options_bracket_watcher",
    "submit_options_bracket_close",
]
