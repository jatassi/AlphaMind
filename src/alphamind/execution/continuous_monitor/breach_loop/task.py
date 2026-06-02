"""Breach-evaluation loop (story 03b / ALP-437).

Long-running asyncio task the monitor supervisor registers as ``breach_loop``.
On each tick (``breach_evaluation_cadence_seconds`` apart):

1. If the market is closed (per :class:`MarketHoursClock`), skip
   evaluation (the watchdog beat happens inside the supervised_loop seam
   before the market-open check, so liveness is maintained on closed ticks).
2. Read open positions + ``DrawdownState`` from the repository, regime
   resolution from the supplied provider, and the underlying-price snapshot
   from :class:`UnderlyingPriceCache`.
3. Call :func:`compose_phase_1_enforcement` → :class:`Phase1EnforcementResult`.
4. Build the per-tick :class:`MarketInputs` and :class:`LibraryConfig`
   (the latter via the injected ``library_config_factory`` so the loop does
   not duplicate the resolver's adapter logic).
5. Call :func:`evaluate_proposals` with an empty proposal list — the library's
   pure projection collapses to "current state against the limits."
6. For each rule's :class:`RuleProjection`, classify the zone via
   :func:`classify_zone` and map the rule's static breach-response
   classification via ``breach_response_lookup``.
7. Detect halt-state transitions via :class:`HaltTransitionTracker` and
   emit ``HALT_ACTIVATED`` / ``HALT_LIFTED`` entries through the supplied
   ``activity_log_sink``.
8. Await ``on_immediate_breach`` for each ``immediate_engine`` rule in
   :class:`RiskZone.BLOCKED`; await ``on_emergency_input`` exactly once with
   the per-tick :class:`BreachLoopResult`.

The loop is the gatekeeper for both downstream paths (story 04a cascade
dispatch + story 04b emergency-invocation evaluation); story 03b's callbacks
are wired by the supervisor and remain no-ops until 04a / 04b land.

Architectural invariants (per parent issue ALP-123):
* Asyncio-only; the loop drives through :data:`SupervisedLoop` (ALP-826)
  which beats the watchdog at the top of every iteration and paces the cadence.
* No re-implementation of breach primitives — every classifier, halt
  computer, and rule-evaluator call site reuses the canonical primitive.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.guardrails import (
    BreachResponse,
    ProgressiveTier,
)
from alphamind.config.models.guardrails import (
    EscalationZones as BreachEscalationZones,
)
from alphamind.execution.continuous_monitor.breach_loop.halt_tracker import (
    HaltTransitionTracker,
)
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopHealthSignal,
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import SupervisedLoop
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    FreshPrice,
    StalePrice,
    UnderlyingPriceCache,
)
from alphamind.execution.guardrail_enforcement import (
    Phase1EnforcementResult,
    compose_phase_1_enforcement,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventSource,
)
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.risk_guardrails.breach_behavior import (
    DrawdownSample,
    HaltState,
    RiskZone,
    classify_zone,
    compute_halt_state,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    EscalationZones,
    IvProvider,
    LibraryConfig,
    MarketInputs,
    RuleProjection,
    evaluate_proposals,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput

log = logging.getLogger(__name__)

# Distinct string that identifies the writer-wedged cold-feed case in the
# health-signal channel (decision D / ALP-831). Separable from the per-ticker-
# lag escalation message so an operator can tell "one ticker lagging" from
# "price stream dead."
_GLOBAL_STALE_ERROR = "underlying price feed globally stale — writer wedged"


class MarketHoursClock(Protocol):
    """Read-only market-hours check the loop pauses against.

    The trading calendar cache in ``alphamind.execution.venue_configuration``
    satisfies this Protocol. Tests substitute a stub that returns whichever
    value the scenario demands.
    """

    def is_market_open(self, at: datetime) -> bool: ...


SnapshotProvider = Callable[[], Awaitable[LibrarySnapshot]]
RegimeProvider = Callable[[], Awaitable[RegimeAdaptationOutput]]
LibraryConfigFactory = Callable[[ActiveRiskParameterSet], LibraryConfig]
ActivityLogSink = Callable[[Iterable[ActivityLogEntry]], Awaitable[None]]
OnImmediateBreach = Callable[[BreachLoopResult, RuleEvaluation], Awaitable[None]]
OnEmergencyInput = Callable[[BreachLoopResult], Awaitable[None]]
# Health-signal sink (ALP-732 Gap 2). Invoked once when the consecutive-failure
# count crosses the configured threshold (``degraded=True``) and once on the
# first successful tick afterwards (``degraded=False``). The monitor wires it
# to its operator surfaces; tests substitute a recorder. Synchronous: the
# monitor-local surfacing (SSE ``put_nowait`` fan-out + logging) does no I/O
# that needs awaiting, so this stays sync rather than adding async-over-sync
# residue (architecture invariant L19).
OnHealthSignal = Callable[[BreachLoopHealthSignal], None]


def _no_op_health_signal(_signal: BreachLoopHealthSignal) -> None:
    """Default health sink — used by callers (e.g. tests) that do not wire one."""


def _safe_emit_health_signal(sink: OnHealthSignal, signal: BreachLoopHealthSignal) -> None:
    """Emit a health signal without letting a faulty sink kill the loop.

    The whole point of the escalation is resilience, so a sink that itself
    raises (e.g. the SSE emitter or alert channel is momentarily down) must not
    take down the breach loop — the failure is logged and the loop continues.
    """
    try:
        sink(signal)
    except Exception:
        log.exception("breach_loop health-signal sink raised; continuing")


async def run_breach_loop(  # noqa: PLR0913,PLR0912,C901 — fan-in is the seam, not incidental complexity
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    repository: PortfolioStateRepository,
    cache: UnderlyingPriceCache,
    snapshot_provider: SnapshotProvider,
    regime_provider: RegimeProvider,
    progressive_tiers: tuple[ProgressiveTier, ...],
    library_config_factory: LibraryConfigFactory,
    iv_provider: IvProvider,
    risk_free_rate: float,
    breach_response_lookup: Mapping[str, BreachResponse],
    market_hours: MarketHoursClock,
    activity_log_sink: ActivityLogSink,
    on_immediate_breach: OnImmediateBreach,
    on_emergency_input: OnEmergencyInput,
    loop: SupervisedLoop,
    on_health_signal: OnHealthSignal = _no_op_health_signal,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Run-forever breach-evaluation loop. See module docstring for the contract.

    The loop drives through *loop* (the supervisor's :meth:`supervised_loop`
    iterator, name + cadence pre-bound by the wiring via ALP-826). The seam
    beats the watchdog at the top of every iteration — including ticks where
    the market is closed — so a wedged breach loop trips ``os._exit(1)`` →
    NSSM restart rather than masquerading as healthy on the process surfaces
    (ALP-819).
    """
    del session  # carried for signature uniformity; the loop reads identity via logging
    failure_threshold = config.breach_loop_consecutive_failure_alert_threshold
    halt_tracker = HaltTransitionTracker()
    entry_counter = {"n": 0}
    # Sustained-failure tracking (ALP-732 Gap 2). ``consecutive_failures``
    # counts back-to-back failed ticks; ``degraded`` debounces the alert so it
    # fires once on crossing the threshold, not on every subsequent failure.
    consecutive_failures = 0
    degraded = False
    # Stale-price tracking (ALP-770). ``stale_consecutive_count`` counts
    # consecutive ticks where at least one open-position ticker had a stale or
    # missing live price; ``stale_degraded`` debounces the escalation alert.
    stale_consecutive_count = 0
    stale_degraded = False

    def _entry_id_factory(local: int) -> str:
        # ``mon-alp-`` prefix mirrors the rest of the continuous-monitor
        # entry vocabulary (``mon-grf-``, ``mon-emt-``, ``mon-brk-``) so
        # operators can ``grep monitor.log | grep '^mon-'`` for the
        # union of monitor-emitted activity-log entries.
        entry_counter["n"] += 1
        return f"mon-alp-{entry_counter['n']:012d}-{local:02d}"

    # The seam beats at the top of every iteration (before the market-open
    # check) so the watchdog sees liveness on closed-market ticks (ALP-819).
    async for _ in loop():
        as_of = now()
        if not market_hours.is_market_open(as_of):
            continue

        try:
            had_stale, expected_tickers = await _run_one_tick(
                as_of=as_of,
                config=config,
                repository=repository,
                cache=cache,
                snapshot_provider=snapshot_provider,
                regime_provider=regime_provider,
                progressive_tiers=progressive_tiers,
                library_config_factory=library_config_factory,
                iv_provider=iv_provider,
                risk_free_rate=risk_free_rate,
                breach_response_lookup=breach_response_lookup,
                halt_tracker=halt_tracker,
                activity_log_sink=activity_log_sink,
                on_immediate_breach=on_immediate_breach,
                on_emergency_input=on_emergency_input,
                entry_id_factory=_entry_id_factory,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Per-tick supervisor per runtime §G1: one bad tick must not kill
            # the loop. ``CancelledError`` re-raised above so supervisor
            # shutdown propagates; ``BaseException`` (``KeyboardInterrupt`` /
            # ``SystemExit``) also propagates as it falls through the
            # ``Exception`` branch.
            log.exception("breach_loop tick raised; sleeping until next cycle")
            consecutive_failures += 1
            if consecutive_failures >= failure_threshold and not degraded:
                degraded = True
                # Loud, named ERROR (ALP-732): the prior signal was an opaque
                # repeating "tick raised", indistinguishable from healthy on the
                # process surfaces. Name the count so the operator sees risk
                # supervision has been down for N ticks running. ``log.exception``
                # attaches the failing tick's traceback to this once-per-episode
                # escalation line so the operator has the error context inline.
                log.exception(
                    "breach_loop DEGRADED: %d consecutive failed ticks "
                    "(threshold=%d); risk supervision is down until a tick succeeds",
                    consecutive_failures,
                    failure_threshold,
                )
                _safe_emit_health_signal(
                    on_health_signal,
                    BreachLoopHealthSignal(
                        degraded=True,
                        consecutive_failures=consecutive_failures,
                        last_error=repr(exc),
                    ),
                )
        else:
            if degraded:
                log.warning(
                    "breach_loop RECOVERED after %d consecutive failed ticks",
                    consecutive_failures,
                )
                degraded = False
                # Emit recovered only when stale-price escalation is also clear;
                # if the stale condition is still active the process is still
                # degraded and a premature recovered signal would mislead the
                # operator surface.
                if not stale_degraded:
                    _safe_emit_health_signal(
                        on_health_signal,
                        BreachLoopHealthSignal(
                            degraded=False,
                            consecutive_failures=consecutive_failures,
                            last_error=None,
                        ),
                    )
            consecutive_failures = 0

            # Stale-price escalation (ALP-770): escalate to DEGRADED after N
            # consecutive ticks where any open-position price was stale or
            # missing; recover when all prices are fresh again.
            if had_stale:
                stale_consecutive_count += 1
                if stale_consecutive_count >= failure_threshold and not stale_degraded:
                    stale_degraded = True
                    log.error(
                        "breach_loop DEGRADED: stale/missing underlying price for %d "
                        "consecutive cycles (threshold=%d); affected positions excluded "
                        "from stop enforcement",
                        stale_consecutive_count,
                        failure_threshold,
                    )
                    _safe_emit_health_signal(
                        on_health_signal,
                        BreachLoopHealthSignal(
                            degraded=True,
                            consecutive_failures=stale_consecutive_count,
                            last_error="stale/missing underlying price",
                        ),
                    )
            else:
                if stale_degraded:
                    log.warning(
                        "breach_loop stale-price RECOVERED after %d consecutive cycles",
                        stale_consecutive_count,
                    )
                    stale_degraded = False
                    # Emit recovered only when tick-failure escalation is also
                    # clear; a concurrent tick-failure DEGRADED keeps the
                    # process degraded even though prices are fresh again.
                    if not degraded:
                        _safe_emit_health_signal(
                            on_health_signal,
                            BreachLoopHealthSignal(
                                degraded=False,
                                consecutive_failures=stale_consecutive_count,
                                last_error=None,
                            ),
                        )
                stale_consecutive_count = 0

            # Global-stale detector (ALP-831, decision D): when the whole feed is
            # cold (writer wedged — every expected ticker STALE/MISSING), emit a
            # distinctly-labelled signal through the same health channel so the
            # operator can differentiate "one ticker lagging" from "price stream
            # dead." This is the single global-stale emitter — 02b/02c add none.
            # ``expected_tickers`` is the open-position set returned by the tick.
            global_signal = cache.global_staleness(
                as_of=as_of,
                max_age_seconds=config.underlying_price_max_age_seconds,
                expected_tickers=expected_tickers,
            )
            if global_signal is not None:
                log.error(
                    "breach_loop: underlying price feed globally stale — "
                    "writer appears wedged; stale=%s missing=%s",
                    sorted(global_signal.stale_tickers),
                    sorted(global_signal.missing_tickers),
                )
                _safe_emit_health_signal(
                    on_health_signal,
                    BreachLoopHealthSignal(
                        degraded=True,
                        consecutive_failures=0,
                        last_error=_GLOBAL_STALE_ERROR,
                    ),
                )


async def _run_one_tick(  # noqa: PLR0913
    *,
    as_of: datetime,
    config: ContinuousMonitorConfig,
    repository: PortfolioStateRepository,
    cache: UnderlyingPriceCache,
    snapshot_provider: SnapshotProvider,
    regime_provider: RegimeProvider,
    progressive_tiers: tuple[ProgressiveTier, ...],
    library_config_factory: LibraryConfigFactory,
    iv_provider: IvProvider,
    risk_free_rate: float,
    breach_response_lookup: Mapping[str, BreachResponse],
    halt_tracker: HaltTransitionTracker,
    activity_log_sink: ActivityLogSink,
    on_immediate_breach: OnImmediateBreach,
    on_emergency_input: OnEmergencyInput,
    entry_id_factory: Callable[[int], str],
) -> tuple[bool, frozenset[str]]:
    """Execute one breach-loop tick.

    Returns a tuple of:
    * ``had_stale_or_missing``: True when at least one open-position ticker
      had a stale or missing live price this tick (excluded from stop
      enforcement); False when all prices were fresh. The caller accumulates
      the count for escalation.
    * ``expected_tickers``: the frozenset of open-position underlying tickers
      the freshness gate checked, so the outer loop can pass them to
      ``cache.global_staleness`` without a second snapshot fetch.
    """
    drawdown_state: DrawdownState = repository.get_drawdown_state()
    regime_output: RegimeAdaptationOutput = await regime_provider()

    phase1_result: Phase1EnforcementResult = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown_state,
        progressive_tiers=progressive_tiers,
    )
    active_risk_parameters = phase1_result.active_risk_parameters

    library_snapshot: LibrarySnapshot = await snapshot_provider()
    library_config = library_config_factory(active_risk_parameters)

    # Per-position freshness gate (ALP-770 / ALP-831): use the shared
    # ``read_all`` API (01b) with ``config.underlying_price_max_age_seconds``
    # so the threshold lives in one place. ``FreshPrice`` entries populate
    # ``underlying_prices``; ``StalePrice``/``MissingPrice`` are excluded from
    # enforcement exactly as before — ``evaluate_proposals`` cannot fire a
    # PRICE_STOP against a frozen sentinel.
    expected_tickers = {ep.underlying for ep in library_snapshot.existing_positions.values()}
    price_reads = cache.read_all(
        expected_tickers,
        as_of=as_of,
        max_age_seconds=config.underlying_price_max_age_seconds,
    )

    underlying_prices: dict[str, float] = {}
    had_stale_or_missing = False
    for ticker, read in price_reads.items():
        if isinstance(read, FreshPrice):
            underlying_prices[ticker] = read.price
        else:
            had_stale_or_missing = True
            if isinstance(read, StalePrice):
                log.warning(
                    "underlying price for %s is stale (%.0fs > max %.0fs); "
                    "excluded from stop enforcement",
                    ticker,
                    read.age_seconds,
                    config.underlying_price_max_age_seconds,
                )
            else:
                log.warning(
                    "no live price received for open-position ticker %s; "
                    "excluded from stop enforcement",
                    ticker,
                )

    market = MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=risk_free_rate,
        iv_provider=iv_provider,
        as_of=as_of,
    )

    library_output = evaluate_proposals(
        state=library_snapshot,
        proposals=(),
        config=library_config,
        market=market,
    )

    rule_evaluations = tuple(
        _build_rule_evaluation(
            projection=projection,
            escalation_zones=library_config.escalation_zones,
            breach_response_lookup=breach_response_lookup,
        )
        for projection in library_output.per_rule
    )

    # ``Phase1EnforcementResult.drawdown_tier`` is the freshly re-classified
    # tier from the current progressive_tiers; ``compute_halt_state`` reads
    # ``drawdown_state.cumulative_tier`` so we override it with the fresh value
    # to avoid relying on the repository's last-write classification.
    classified_drawdown_state = dataclasses.replace(
        drawdown_state, cumulative_tier=phase1_result.drawdown_tier
    )
    halt_state: HaltState | None = compute_halt_state(
        drawdown_state=classified_drawdown_state,
        active_risk_parameters=active_risk_parameters,
    )

    immediate_action_breaches = tuple(
        e for e in rule_evaluations if e.classification is BreachResponse.immediate_engine
    )

    sample = DrawdownSample(
        sampled_at=as_of,
        intraday_drawdown_pct=drawdown_state.intraday_drawdown_pct,
    )
    result = BreachLoopResult(
        as_of=as_of,
        phase1_result=phase1_result,
        rule_evaluations=rule_evaluations,
        halt_state=halt_state,
        immediate_action_breaches=immediate_action_breaches,
        drawdown_velocity_sample=sample,
    )

    # Halt-transition events fire first so the activity-log sink sees the
    # correct ordering relative to downstream callbacks.
    cumulative_full_halt_limit_pct = _resolve_full_halt_limit(progressive_tiers)
    entries = tuple(
        halt_tracker.observe(
            halt_state=halt_state,
            invocation_id=f"monitor-{as_of.isoformat()}",
            at=as_of,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=entry_id_factory,
            current_daily_drawdown_pct=drawdown_state.intraday_drawdown_pct,
            current_cumulative_drawdown_pct=drawdown_state.current_drawdown_pct,
            cumulative_full_halt_limit_pct=cumulative_full_halt_limit_pct,
        )
    )
    await activity_log_sink(entries)

    for evaluation in immediate_action_breaches:
        if evaluation.zone is RiskZone.BLOCKED:
            await on_immediate_breach(result, evaluation)

    await on_emergency_input(result)

    return had_stale_or_missing, frozenset(expected_tickers)


def _build_rule_evaluation(
    *,
    projection: RuleProjection,
    escalation_zones: Mapping[str, EscalationZones],
    breach_response_lookup: Mapping[str, BreachResponse],
) -> RuleEvaluation:
    """Project one ``RuleProjection`` into a ``RuleEvaluation``.

    For floor rules (``inverse=True``, e.g., ``min_cash_reserve_pct``), the
    library's projection compares ``limit - current`` rather than ``current -
    limit``; for the breach loop's reporting convention we keep ``overage``
    signed in the "above limit" direction. Sector-concentration rules expose
    a generated ID like ``sector_concentration_tech``; the breach-response
    lookup keys on the bare rule ID ``sector_concentration_pct`` so we strip
    the trailing sector suffix.
    """
    rule_id = projection.rule
    escalation_key = rule_id if rule_id in escalation_zones else _strip_sector_suffix(rule_id)
    zones = escalation_zones[escalation_key]
    # The library carves a dataclass ``EscalationZones`` with float fields for
    # determinism-test hashing; ``classify_zone`` consumes the breach-behavior
    # Pydantic ``EscalationZones`` with int fields. Convert here; both shapes
    # carry identical semantic content per ``from_resolved_config``'s adapter.
    breach_zones = BreachEscalationZones(
        warning=int(zones.warning),
        critical=int(zones.critical),
        hard_block=int(zones.hard_block),
    )
    zone = classify_zone(
        current_value=abs(projection.current),
        limit_value=projection.limit,
        escalation_zones=breach_zones,
    )

    classification_key = _strip_sector_suffix(rule_id)
    classification: BreachResponse | None = None
    if zone is RiskZone.BLOCKED:
        classification = breach_response_lookup.get(classification_key)

    overage = (
        projection.limit - projection.current
        if projection.inverse
        else projection.current - projection.limit
    )

    return RuleEvaluation(
        rule_id=rule_id,
        current_value=projection.current,
        limit_value=projection.limit,
        overage=overage,
        zone=zone,
        classification=classification,
        breaching_position_id=projection.breaching_position_id,
    )


def _strip_sector_suffix(rule_id: str) -> str:
    """Map ``sector_concentration_<sector>`` back to ``sector_concentration_pct``.

    Other rule IDs pass through unchanged. The library generates one rule per
    active sector (e.g., ``sector_concentration_tech``); the breach-response
    classification lives on the registry entry ``sector_concentration_pct``.
    """
    if rule_id.startswith("sector_concentration_") and rule_id != "sector_concentration_pct":
        return "sector_concentration_pct"
    return rule_id


def _resolve_full_halt_limit(progressive_tiers: tuple[ProgressiveTier, ...]) -> float | None:
    """Look up the cumulative-drawdown ``FULL_HALT`` trigger from the tier list.

    The cumulative-drawdown rule defines its tiers via
    :class:`ProgressiveTier`; the highest trigger that carries
    ``full_halt=True`` is the threshold the breach loop reports against. When
    no tier carries ``full_halt`` (e.g., a stripped config), returns ``None``.
    """
    full_halt_triggers = [tier.trigger_pct for tier in progressive_tiers if tier.full_halt]
    if not full_halt_triggers:
        return None
    return max(full_halt_triggers)
