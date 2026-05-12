"""Breach-evaluation loop (story 03b / ALP-437).

Long-running asyncio task the monitor supervisor registers as ``breach_loop``.
On each tick (``breach_evaluation_cadence_seconds`` apart):

1. If the market is closed (per :class:`MarketHoursClock`), sleep and skip
   evaluation.
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
9. Sleep ``breach_evaluation_cadence_seconds`` until the next tick.

The loop is the gatekeeper for both downstream paths (story 04a cascade
dispatch + story 04b emergency-invocation evaluation); story 03b's callbacks
are wired by the supervisor and remain no-ops until 04a / 04b land.

Architectural invariants (per parent issue ALP-123):
* Asyncio-only; the loop uses ``asyncio.sleep`` between ticks and never
  blocks the event loop.
* No re-implementation of breach primitives — every classifier, halt
  computer, and rule-evaluator call site reuses the canonical primitive.
"""

from __future__ import annotations

import asyncio
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
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
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
    evaluate_proposals,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput

log = logging.getLogger(__name__)


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


async def run_breach_loop(  # noqa: PLR0913 — fan-in is the seam, not incidental complexity
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
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Run-forever breach-evaluation loop. See module docstring for the contract."""
    del session  # carried for signature uniformity; the loop reads identity via logging
    cadence_seconds = float(config.breach_evaluation_cadence_seconds)
    halt_tracker = HaltTransitionTracker()
    entry_counter = {"n": 0}

    def _entry_id_factory(local: int) -> str:
        entry_counter["n"] += 1
        return f"alp-{entry_counter['n']:012d}-{local:02d}"

    while True:
        as_of = now()
        if not market_hours.is_market_open(as_of):
            await asyncio.sleep(cadence_seconds)
            continue

        try:
            await _run_one_tick(
                as_of=as_of,
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
        except BaseException:
            log.exception("breach_loop tick raised; sleeping until next cycle")

        await asyncio.sleep(cadence_seconds)


async def _run_one_tick(  # noqa: PLR0913
    *,
    as_of: datetime,
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
) -> None:
    """Execute one breach-loop tick."""
    drawdown_state: DrawdownState = await repository.get_drawdown_state()
    regime_output: RegimeAdaptationOutput = await regime_provider()

    phase1_result: Phase1EnforcementResult = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown_state,
        progressive_tiers=progressive_tiers,
    )
    active_risk_parameters = phase1_result.active_risk_parameters

    library_snapshot: LibrarySnapshot = await snapshot_provider()
    library_config = library_config_factory(active_risk_parameters)

    underlying_prices = {ticker: quote.price for ticker, quote in cache.get_all().items()}
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
            rule_id=projection.rule,
            current_value=projection.current,
            limit_value=projection.limit,
            inverse=projection.inverse,
            escalation_zones=library_config.escalation_zones,
            breach_response_lookup=breach_response_lookup,
        )
        for projection in library_output.per_rule
    )

    # ``Phase1EnforcementResult.drawdown_tier`` is the freshly re-classified
    # tier from the current progressive_tiers; ``compute_halt_state`` reads
    # ``drawdown_state.cumulative_tier`` so we override it with the fresh value
    # to avoid relying on the repository's last-write classification.
    classified_drawdown_state = drawdown_state.model_copy(
        update={"cumulative_tier": phase1_result.drawdown_tier}
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


def _build_rule_evaluation(
    *,
    rule_id: str,
    current_value: float,
    limit_value: float,
    inverse: bool,
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
        current_value=abs(current_value),
        limit_value=limit_value,
        escalation_zones=breach_zones,
    )

    classification_key = _strip_sector_suffix(rule_id)
    classification: BreachResponse | None = None
    if zone is RiskZone.BLOCKED:
        classification = breach_response_lookup.get(classification_key)

    overage = limit_value - current_value if inverse else current_value - limit_value

    return RuleEvaluation(
        rule_id=rule_id,
        current_value=current_value,
        limit_value=limit_value,
        overage=overage,
        zone=zone,
        classification=classification,
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
