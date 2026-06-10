"""Imperative shell for the isolated safety core (ALP-857 / ADR-0004).

The shell: each tick it beats the file heartbeat, loads the broker snapshot
(positions + account equity — the Broker-Owned Fact) and the live price reads,
calls the pure :func:`evaluate_safety` core, then surfaces breaches + staleness
loudly (logging + an optional signal sink). It is a *minimal, non-blocking*
process: it owns its own tiny beat-at-top loop and does NOT use the monitor's
``MonitorSupervisor`` — that is a monitor internal and a loop-resident watchdog,
exactly the topology ADR-0004 escapes. The safety core's liveness is owned by
the dedicated out-of-process watchdog reading the heartbeat file.

Writes-nothing invariant: the shell has no DB session and no broker-submit port
in scope. It reads the broker snapshot (read-only ``get_positions`` /
``get_account``) and the in-process price cache, beats a file heartbeat, and
logs — nothing else. A DB write is unreachable by construction (ADR-0004 /
ADR-0005).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime

from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.continuous_monitor.safety_core.evaluation import (
    SafetyLimits,
    evaluate_safety,
)
from alphamind.execution.continuous_monitor.safety_core.records import (
    SafetyEvaluation,
    SafetyPosition,
)
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.execution.process_supervision import HeartbeatSink

log = logging.getLogger(__name__)

# Read-only broker ports (the broker API boundary). Sync because the underlying
# alpaca-py TradingClient is synchronous; the shell calls them off the loop body.
GetPositions = Callable[[], tuple[PositionSnapshot, ...]]
GetAccount = Callable[[], TradeAccountSnapshot]
# A zero-arg factory returning the tick iterator (name/cadence pre-bound). Mirrors
# the supervisor's SupervisedLoop seam WITHOUT importing it (the safety core
# imports no monitor internals). Tests bind a bounded iterator.
SafetyLoop = Callable[[], AsyncIterator[None]]
# A non-DB sink for each tick's evaluation (operator surfaces / tests). Optional.
OnEvaluation = Callable[[SafetyEvaluation], None]


def _project_positions(snapshots: tuple[PositionSnapshot, ...]) -> tuple[SafetyPosition, ...]:
    """Project broker ``PositionSnapshot`` rows into the core's minimal shape.

    Equities only carry an underlying ticker the price stream covers; the
    options-leg market value still counts toward gross exposure, so every
    snapshot is projected. ``market_value`` is the broker's signed value
    (``signed_money`` → ``Decimal``); cast to float at the boundary.
    """
    return tuple(
        SafetyPosition(
            symbol=snap.symbol,
            market_value=float(snap.market_value),
            side=snap.side,
        )
        for snap in snapshots
    )


async def run_safety_core(  # noqa: PLR0913 — composition root: every arg is one injected port
    *,
    get_positions: GetPositions,
    get_account: GetAccount,
    price_cache: UnderlyingPriceCache,
    heartbeat: HeartbeatSink,
    limits: SafetyLimits,
    max_age_seconds: float,
    loop: SafetyLoop,
    now: Callable[[], datetime],
    on_evaluation: OnEvaluation | None = None,
) -> None:
    """Run-forever safety-core loop. See module docstring for the contract.

    Beats the heartbeat at the **top** of every iteration — before the broker /
    price reads — so a read that wedges still leaves a fresh-enough last beat for
    exactly one bound, then goes stale and the out-of-process watchdog restarts
    the process. A per-tick try/except keeps one bad tick (a broker hiccup) from
    killing the loop; ``CancelledError`` propagates so supervised shutdown works.
    """
    async for _ in loop():
        heartbeat.beat()
        try:
            await _run_one_tick(
                get_positions=get_positions,
                get_account=get_account,
                price_cache=price_cache,
                limits=limits,
                max_age_seconds=max_age_seconds,
                now=now,
                on_evaluation=on_evaluation,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("safety_core tick raised; continuing to next cycle")


async def _run_one_tick(
    *,
    get_positions: GetPositions,
    get_account: GetAccount,
    price_cache: UnderlyingPriceCache,
    limits: SafetyLimits,
    max_age_seconds: float,
    now: Callable[[], datetime],
    on_evaluation: OnEvaluation | None,
) -> None:
    """Execute one safety-core tick: load → evaluate → surface."""
    as_of = now()
    snapshots = get_positions()
    account = get_account()
    positions = _project_positions(snapshots)
    expected_tickers = {p.symbol for p in positions}
    price_reads = price_cache.read_all(
        expected_tickers,
        as_of=as_of,
        max_age_seconds=max_age_seconds,
    )

    evaluation = evaluate_safety(
        as_of=as_of,
        equity=float(account.equity),
        positions=positions,
        price_reads=price_reads,
        limits=limits,
    )

    _surface(evaluation)
    if on_evaluation is not None:
        on_evaluation(evaluation)


def _surface(evaluation: SafetyEvaluation) -> None:
    """Log breaches + cold-feed staleness loudly (the operator-visible surface).

    The safety core acts by being *loud and alive*, not by writing: positions
    are broker-protected by the floor (ADR-0004), so the core's job is detection
    + liveness. It logs; it does not submit or persist.
    """
    if evaluation.breached_rules:
        log.critical(
            "safety_core BREACH: broker snapshot breaches %s at %s",
            ", ".join(evaluation.breached_rules),
            evaluation.as_of.isoformat(),
        )
    if evaluation.price_staleness.globally_stale:
        log.error(
            "safety_core: underlying price feed globally stale — writer appears "
            "wedged; stale=%s missing=%s",
            sorted(evaluation.price_staleness.stale),
            sorted(evaluation.price_staleness.missing),
        )
