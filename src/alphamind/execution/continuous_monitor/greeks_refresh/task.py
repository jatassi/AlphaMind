"""Greeks-refresh run-forever task (story 03a / ALP-436).

Long-running asyncio task the monitor supervisor registers as
``greeks_refresh``. Each inspection cycle:

1. Reads the open option / strategy positions from the repository.
2. Snapshots the underlying-price cache once for the cycle (cheap; in-memory).
3. For each position, evaluates the dual trigger predicate:
   * scheduled: ``now - last_refreshed_at >= greeks_refresh_interval_minutes``
   * move-based: ``|spot - anchor| / anchor >= greeks_refresh_underlying_move_threshold_pct``
4. For positions that triggered, fetches IV in a single batched query, computes
   fresh greeks via :func:`recompute_greeks` / :func:`recompute_strategy_greeks`,
   and persists via the injected :class:`GreeksWriter`. Failures (no row,
   provider exception) preserve prior greeks, flip ``refresh_failed=True``,
   and emit a ``GREEKS_REFRESH_FAILED`` activity-log entry.
5. Outside market hours (``market_open=False``), the cycle is a no-op — the
   loop simply sleeps until the next inspection tick.

Determinism + testability seams:

* ``now`` is a callable returning a ``datetime`` so tests inject a fixed clock.
* ``risk_free_rate_provider`` is a callable so the rate is read once per
  cycle and tests can swap a constant.
* ``market_open`` is a callable so tests can short-circuit the venue
  calendar; production wires it to
  ``alphamind.execution.venue_configuration.calendar_cache.TradingCalendarCache.is_market_open``.
* ``iv_fetch`` and ``writer`` and ``activity_log`` are injected protocols /
  callables so tests substitute in-memory fakes for the entire write path.

The architecture-doc retry-on-failure semantics are realised through the
``iv_fetch`` shape itself: the production wiring layers
``submit_with_retry`` over a direct query, so the kernel here treats one
``iv_fetch`` call as the "exhausted retry budget" envelope. The kernel does
NOT re-call ``iv_fetch`` on failure — that would double-retry.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.greeks_refresh.iv_provider import IVQuote
from alphamind.execution.continuous_monitor.greeks_refresh.recompute import (
    recompute_greeks,
    recompute_strategy_greeks,
)
from alphamind.execution.continuous_monitor.greeks_refresh.state import (
    LastRefreshState,
    seed_last_refresh_states,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    GreeksRefreshFailedDetail,
)
from alphamind.portfolio_state.records.positions import (
    OptionGreeks,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
    occ_symbol_for_options,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Injected collaborator types
# ---------------------------------------------------------------------------


IVFetcher = Callable[[Iterable[str]], Awaitable[dict[str, IVQuote]]]
ActivityLogEmitter = Callable[[ActivityLogEntry], Awaitable[None]]
NowProvider = Callable[[], datetime]
# Async-native: the providers run inside the supervisor's loop, so the
# wiring layer can hit the DB without bridging through ``asyncio.run`` (which
# fell through to a hard-coded fallback when invoked from the running loop).
RiskFreeRateProvider = Callable[[], Awaitable[float]]
MarketOpenPredicate = Callable[[datetime], bool]
InvocationIdProvider = Callable[[], Awaitable[str]]
SleepCallable = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _CycleContext:
    """Bundle of shared inputs every per-position branch reads.

    Threading these as a single frozen value keeps the per-branch helper
    signatures within the project's argument-count budget and groups the
    "what" of this cycle (now, invocation_id, risk_free_rate, IV quotes,
    failure-reason envelope) from the "where" (writer + activity-log +
    state map). The state map is intentionally outside the context — it
    is mutable and lifetime-scoped to the task, not the cycle.
    """

    iv_quotes: dict[str, IVQuote]
    fetch_failure_reason: str | None
    risk_free_rate: float
    now: datetime
    invocation_id: str


@runtime_checkable
class GreeksWriter(Protocol):
    """Narrow writer surface for the refresh task.

    Two methods because the persisted shape differs between single-leg
    options positions (one ``OptionGreeks`` overwrite) and multi-leg
    strategy positions (per-leg greeks + aggregated ``strategy_greeks``).
    Production wiring lives in :class:`SqlGreeksWriter`; tests pass a
    capturing fake.
    """

    async def update_options_greeks(self, *, position_id: str, greeks: OptionGreeks) -> None:
        """Replace the position's ``OptionsPositionDetails.greeks`` with *greeks*."""

    async def update_strategy_greeks(
        self,
        *,
        position_id: str,
        per_leg: dict[str, OptionGreeks],
        aggregated: OptionGreeks,
    ) -> None:
        """Replace each leg's ``OptionGreeks`` and the strategy-level
        ``strategy_greeks`` in one atomic write."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _spot_for_position(position: PositionRecord, cache: UnderlyingPriceCache) -> float | None:
    details = position.details
    if isinstance(details, OptionsPositionDetails):
        ticker = details.underlying_ticker
    elif isinstance(details, StrategyPositionDetails) and details.legs:
        ticker = details.legs[0].options.underlying_ticker
    else:
        return None
    quote = cache.get(ticker)
    return None if quote is None else quote.price


def _is_due(
    *,
    state: LastRefreshState,
    spot: float,
    now: datetime,
    interval_minutes: int,
    move_threshold_pct: float,
) -> bool:
    """Dual-trigger predicate: scheduled OR move-based.

    Scheduled: ``now - last_refreshed_at >= interval_minutes``. Move-based:
    ``|spot - anchor| / anchor * 100 >= move_threshold_pct``. When the
    anchor price is zero (cold-start — the underlying-price cache has not
    yet observed a quote), the move predicate is skipped and only the
    scheduled trigger fires.
    """
    elapsed_min = (now - state.last_refreshed_at).total_seconds() / 60.0
    if elapsed_min >= interval_minutes:
        return True
    anchor = state.underlying_price_at_last_refresh
    if anchor <= 0.0:
        return False
    move_pct = abs(spot - anchor) / anchor * 100.0
    return move_pct >= move_threshold_pct


def _build_failed_greeks(prior: OptionGreeks) -> OptionGreeks:
    """Construct a new ``OptionGreeks`` preserving prior values with refresh_failed=True.

    Honours the architecture's "preserve prior greeks" semantics: the four
    greeks plus ``as_of_timestamp`` and ``iv_used`` are copied verbatim,
    only ``refresh_failed`` flips to ``True``. Downstream consumers widen
    the derivation-uncertainty buffer when they observe the flag.
    """
    return OptionGreeks(
        delta=prior.delta,
        gamma=prior.gamma,
        theta=prior.theta,
        vega=prior.vega,
        as_of_timestamp=prior.as_of_timestamp,
        iv_used=prior.iv_used,
        refresh_failed=True,
    )


def _build_failure_entry(
    *,
    position: PositionRecord,
    underlying_ticker: str,
    occ_symbol: str,
    failure_reason: str,
    prior_as_of: datetime | None,
    now: datetime,
    invocation_id: str,
) -> ActivityLogEntry:
    """Compose the typed ``ActivityLogEntry`` the activity-log emitter consumes.

    ``entry_id`` carries a short random suffix so concurrent emissions
    within one cycle do not collide. ``source`` is ``GUARDRAIL_LAYER`` — the
    same value the emergency-invocation trigger uses (parent issue ALP-123
    § Surfacing condition (iii); story 03a stays within the existing
    vocabulary). ``invocation_id`` references the last committed invocation
    so the activity-log FK to ``invocations`` holds.
    """
    suffix = secrets.token_hex(4)
    entry_id = f"mon-grf-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{suffix}"
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=now,
        event_type=EventType.GREEKS_REFRESH_FAILED,
        event_group=EventGroup.RISK_AND_GUARDRAIL,
        position_id=position.position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER,
        detail=GreeksRefreshFailedDetail(
            underlying_ticker=underlying_ticker,
            occ_symbol=occ_symbol,
            failure_reason=failure_reason,
            prior_as_of=prior_as_of,
        ),
    )


# ---------------------------------------------------------------------------
# Single-cycle kernel
# ---------------------------------------------------------------------------


_DueEntry = tuple[PositionRecord, float, str]


def _select_due_positions(
    positions: tuple[PositionRecord, ...],
    *,
    cache: UnderlyingPriceCache,
    states: dict[str, LastRefreshState],
    now: datetime,
    interval_minutes: int,
    move_threshold_pct: float,
) -> list[_DueEntry]:
    """Filter open positions to those due for refresh this cycle.

    Returns a list of ``(position, spot, underlying_ticker)`` triples. The
    state map is seeded for any never-before-seen positions and the
    trigger predicate is evaluated against the per-position anchor.
    """
    cache_snapshot = {ticker: q.price for ticker, q in cache.get_all().items()}
    for position in positions:
        if position.position_id in states:
            continue
        states.update(
            seed_last_refresh_states((position,), underlying_prices=cache_snapshot, now=now)
        )

    due: list[_DueEntry] = []
    for position in positions:
        details = position.details
        if not isinstance(details, OptionsPositionDetails | StrategyPositionDetails):
            continue
        spot = _spot_for_position(position, cache)
        if spot is None or spot <= 0.0:
            continue
        state = states.get(position.position_id)
        if state is None:
            continue
        if not _is_due(
            state=state,
            spot=spot,
            now=now,
            interval_minutes=interval_minutes,
            move_threshold_pct=move_threshold_pct,
        ):
            continue
        underlying_ticker = (
            details.underlying_ticker
            if isinstance(details, OptionsPositionDetails)
            else details.legs[0].options.underlying_ticker
        )
        due.append((position, spot, underlying_ticker))
    return due


def _collect_occ_symbols(due: list[_DueEntry]) -> set[str]:
    """Build the unique OCC-symbol set from a list of due positions."""
    symbols: set[str] = set()
    for position, _spot, _underlying in due:
        details = position.details
        if isinstance(details, OptionsPositionDetails):
            symbols.add(occ_symbol_for_options(details))
        elif isinstance(details, StrategyPositionDetails):
            symbols.update(occ_symbol_for_options(leg.options) for leg in details.legs)
    return symbols


async def _fetch_quotes_with_failure_envelope(
    iv_fetch: IVFetcher,
    symbols: set[str],
) -> tuple[dict[str, IVQuote], str | None]:
    """Run the batch IV fetch under a try/except guard.

    Returns ``(quotes, failure_reason)``. On success ``failure_reason`` is
    ``None`` and the per-position branches consume the quotes dictionary;
    on exception every due position is routed through the failure path
    with the surfaced reason tag.
    """
    try:
        quotes = await iv_fetch(symbols)
    except Exception:
        # Batch-fetch supervisor per runtime §G1: any IV-provider failure
        # routes every due position to the failure path with a stable reason
        # tag, so the cycle stays observable instead of crashing. The IV
        # provider hides multiple vendor / DB exception types behind one
        # call; surfacing them individually would couple this kernel to the
        # provider implementation. ``BaseException`` (``CancelledError``)
        # propagates so supervisor shutdown is honored.
        log.exception("greeks_refresh batch IV fetch raised; routing positions to failure")
        return {}, "iv_fetch_db_error"
    return quotes, None


async def _run_refresh_cycle(  # noqa: PLR0913 — kernel exposes every collaborator as an injectable seam for tests
    *,
    config: ContinuousMonitorConfig,
    repository: OpenPositionsReader,
    cache: UnderlyingPriceCache,
    states: dict[str, LastRefreshState],
    iv_fetch: IVFetcher,
    writer: GreeksWriter,
    activity_log: ActivityLogEmitter,
    risk_free_rate: float,
    now: datetime,
    invocation_id_provider: InvocationIdProvider,
    market_open: bool,
) -> None:
    """One inspection-cycle pass — public for testability.

    Off-hours short-circuit: when ``market_open`` is ``False`` the kernel
    returns immediately without touching the cache, IV provider, writer,
    or activity log. The architecture's "theta still accumulates via Δt"
    invariant holds purely because no greeks are written; the next in-session
    cycle observes the new ``Δt = (now - prior_as_of)`` and the closed-form
    handles theta correctly.
    """
    if not market_open:
        return

    positions = await repository.get_open_positions()
    if not positions:
        return

    due = _select_due_positions(
        positions,
        cache=cache,
        states=states,
        now=now,
        interval_minutes=config.greeks_refresh_interval_minutes,
        move_threshold_pct=config.greeks_refresh_underlying_move_threshold_pct,
    )
    if not due:
        return

    iv_quotes, fetch_failure_reason = await _fetch_quotes_with_failure_envelope(
        iv_fetch, _collect_occ_symbols(due)
    )
    ctx = _CycleContext(
        iv_quotes=iv_quotes,
        fetch_failure_reason=fetch_failure_reason,
        risk_free_rate=risk_free_rate,
        now=now,
        invocation_id=await invocation_id_provider(),
    )

    for position, spot, underlying_ticker in due:
        details = position.details
        if isinstance(details, OptionsPositionDetails):
            await _refresh_options_position(
                position=position,
                details=details,
                spot=spot,
                underlying_ticker=underlying_ticker,
                writer=writer,
                activity_log=activity_log,
                states=states,
                ctx=ctx,
            )
        elif isinstance(details, StrategyPositionDetails):
            await _refresh_strategy_position(
                position=position,
                details=details,
                spot=spot,
                underlying_ticker=underlying_ticker,
                writer=writer,
                activity_log=activity_log,
                states=states,
                ctx=ctx,
            )


async def _refresh_options_position(
    *,
    position: PositionRecord,
    details: OptionsPositionDetails,
    spot: float,
    underlying_ticker: str,
    writer: GreeksWriter,
    activity_log: ActivityLogEmitter,
    states: dict[str, LastRefreshState],
    ctx: _CycleContext,
) -> None:
    """Refresh a single-leg options position end-to-end (success or failure)."""
    occ = occ_symbol_for_options(details)
    failure_reason: str | None = ctx.fetch_failure_reason
    if failure_reason is None and ctx.iv_quotes.get(occ) is None:
        failure_reason = "iv_fetch_no_row"
    if failure_reason is not None:
        await _emit_failure(
            position=position,
            underlying_ticker=underlying_ticker,
            occ_symbol=occ,
            failure_reason=failure_reason,
            prior_greeks=details.greeks,
            writer=writer,
            activity_log=activity_log,
            states=states,
            spot=spot,
            ctx=ctx,
        )
        return
    quote = ctx.iv_quotes[occ]
    fresh = recompute_greeks(
        position=details,
        iv=quote.iv,
        spot=spot,
        as_of=ctx.now,
        risk_free_rate=ctx.risk_free_rate,
    )
    await writer.update_options_greeks(position_id=position.position_id, greeks=fresh)
    states[position.position_id] = LastRefreshState(
        position_id=position.position_id,
        last_refreshed_at=ctx.now,
        underlying_price_at_last_refresh=spot,
    )


async def _refresh_strategy_position(
    *,
    position: PositionRecord,
    details: StrategyPositionDetails,
    spot: float,
    underlying_ticker: str,
    writer: GreeksWriter,
    activity_log: ActivityLogEmitter,
    states: dict[str, LastRefreshState],
    ctx: _CycleContext,
) -> None:
    """Refresh a multi-leg strategy position end-to-end.

    Failure semantics: if any leg's IV is missing, the entire strategy
    refresh fails and prior greeks are preserved. This mirrors the
    OPEN/ADD-time validation behavior — a strategy is atomic with respect
    to greeks consistency.
    """
    occ_symbols = {leg.leg_id: occ_symbol_for_options(leg.options) for leg in details.legs}
    if ctx.fetch_failure_reason is not None:
        first_leg_symbol = occ_symbols[details.legs[0].leg_id] if details.legs else ""
        await _emit_failure(
            position=position,
            underlying_ticker=underlying_ticker,
            occ_symbol=first_leg_symbol,
            failure_reason=ctx.fetch_failure_reason,
            prior_greeks=details.strategy_greeks,
            writer=writer,
            activity_log=activity_log,
            states=states,
            spot=spot,
            ctx=ctx,
        )
        return
    leg_ivs: dict[str, float] = {}
    for leg in details.legs:
        sym = occ_symbols[leg.leg_id]
        quote = ctx.iv_quotes.get(sym)
        if quote is None:
            await _emit_failure(
                position=position,
                underlying_ticker=underlying_ticker,
                occ_symbol=sym,
                failure_reason="iv_fetch_no_row",
                prior_greeks=details.strategy_greeks,
                writer=writer,
                activity_log=activity_log,
                states=states,
                spot=spot,
                ctx=ctx,
            )
            return
        leg_ivs[leg.leg_id] = quote.iv
    per_leg, aggregated = recompute_strategy_greeks(
        strategy=details,
        leg_ivs=leg_ivs,
        spot=spot,
        as_of=ctx.now,
        risk_free_rate=ctx.risk_free_rate,
    )
    await writer.update_strategy_greeks(
        position_id=position.position_id,
        per_leg=per_leg,
        aggregated=aggregated,
    )
    states[position.position_id] = LastRefreshState(
        position_id=position.position_id,
        last_refreshed_at=ctx.now,
        underlying_price_at_last_refresh=spot,
    )


async def _emit_failure(  # noqa: PLR0913 — fan-out parameters for the failure path
    *,
    position: PositionRecord,
    underlying_ticker: str,
    occ_symbol: str,
    failure_reason: str,
    prior_greeks: OptionGreeks,
    writer: GreeksWriter,
    activity_log: ActivityLogEmitter,
    states: dict[str, LastRefreshState],
    spot: float,
    ctx: _CycleContext,
) -> None:
    """Failure path: preserve prior greeks, flip ``refresh_failed=True``, emit
    the activity-log entry. Used by both single-leg + strategy paths.

    For strategy positions, the writer is called via ``update_strategy_greeks``
    with the prior per-leg greeks preserved and the aggregated greeks set to
    the failure variant. This keeps the persisted strategy in lockstep with
    the per-leg snapshot — no torn state across the leg boundary.

    Bumps the position's ``LastRefreshState.last_refreshed_at`` to ``ctx.now``
    and resets ``underlying_price_at_last_refresh`` to the current spot so the
    next *scheduled* trigger waits a full interval. Without this, every
    inspection cycle would re-evaluate the failing position, re-emit a
    ``GREEKS_REFRESH_FAILED`` entry, and flood the activity log indefinitely
    — the position can still re-fire mid-interval via the move-trigger if
    spot drifts past the threshold.
    """
    details = position.details
    failed = _build_failed_greeks(prior_greeks)
    if isinstance(details, OptionsPositionDetails):
        await writer.update_options_greeks(position_id=position.position_id, greeks=failed)
    elif isinstance(details, StrategyPositionDetails):
        per_leg_failed = {
            leg.leg_id: _build_failed_greeks(leg.options.greeks) for leg in details.legs
        }
        await writer.update_strategy_greeks(
            position_id=position.position_id,
            per_leg=per_leg_failed,
            aggregated=failed,
        )
    entry = _build_failure_entry(
        position=position,
        underlying_ticker=underlying_ticker,
        occ_symbol=occ_symbol,
        failure_reason=failure_reason,
        prior_as_of=prior_greeks.as_of_timestamp,
        now=ctx.now,
        invocation_id=ctx.invocation_id,
    )
    await activity_log(entry)
    states[position.position_id] = LastRefreshState(
        position_id=position.position_id,
        last_refreshed_at=ctx.now,
        underlying_price_at_last_refresh=spot,
    )


# ---------------------------------------------------------------------------
# Run-forever entry point
# ---------------------------------------------------------------------------


async def run_greeks_refresh(  # noqa: PLR0913 — orchestrator surface dictated by parent issue ALP-123 § decision (C)
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    repository: OpenPositionsReader,
    cache: UnderlyingPriceCache,
    iv_fetch: IVFetcher,
    writer: GreeksWriter,
    activity_log: ActivityLogEmitter,
    risk_free_rate_provider: RiskFreeRateProvider,
    now: NowProvider = lambda: datetime.now(UTC),
    market_open: MarketOpenPredicate = lambda _: True,
    invocation_id_provider: InvocationIdProvider,
    sleep: SleepCallable = asyncio.sleep,
) -> None:
    """Long-running task the supervisor registers as ``greeks_refresh``.

    The loop body delegates to :func:`_run_refresh_cycle` so the same code
    path the tests exercise drives the production loop. State is private to
    this task — the ``LastRefreshState`` map lives in the closure and is
    seeded lazily on the first cycle (so a freshly-restarted monitor picks
    up positions that were opened during the gap).
    """
    del session  # session identity flows through invocation_id_provider closure
    states: dict[str, LastRefreshState] = {}
    while True:
        current_now = now()
        try:
            await _run_refresh_cycle(
                config=config,
                repository=repository,
                cache=cache,
                states=states,
                iv_fetch=iv_fetch,
                writer=writer,
                activity_log=activity_log,
                risk_free_rate=await risk_free_rate_provider(),
                now=current_now,
                invocation_id_provider=invocation_id_provider,
                market_open=market_open(current_now),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Per-cycle supervisor per runtime §G1: the cycle should not raise
            # — each per-position branch handles its own failures. A raise
            # here indicates a programming bug; log and continue so the loop
            # survives transient consistency issues. ``BaseException``
            # (``CancelledError``) re-raised above for clean shutdown.
            log.exception("greeks_refresh cycle raised; continuing after sleep")
        await sleep(float(config.greeks_refresh_inspection_cadence_seconds))
