"""Engine-envelope cascade dispatcher (story 04a / ALP-438).

The :class:`CascadeDispatcher` is the ``on_immediate_breach`` callback the
breach loop awaits. Per immediate-action ``HARD_BLOCK`` breach:

1. Resolve the per-rule selector from :data:`RULE_SELECTOR_DISPATCH`.
2. Pull the per-tick :class:`BreachDispatchContext` from the injected provider.
3. Build the selector's keyword arguments via the caller-supplied per-rule
   provider, invoke the selector, and translate its
   :class:`PositionSelectionResult` into a :class:`ProposedClose`.
4. Route through :func:`orchestrate_breach_cascade` — which handles the
   secondary-breach check, the alternate-position search, and chains
   follow-up envelopes under a shared :func:`generate_cascade_id`.
5. For each returned :class:`EngineEnvelope`:
   - If its secondary-breach-check result is ``deferred_to_pm`` and no
     alternate cleared it, emit a :class:`DeferralEvent` via the injected
     ``deferral_sink`` and skip submission (avoids the OMS reject path).
   - Otherwise, translate to the OMS envelope shape and submit via the
     injected ``submit_envelope`` callable.

Margin-call cascades arrive via a separate entry, :meth:`handle_margin_call`
(invoked from the broker-fill or account-state paths — not the breach loop)
and route through :func:`orchestrate_margin_call_cascade` instead.

The dispatcher never calls the LLM, never touches position state directly,
and never bypasses the breach-behavior primitives — its sole responsibility
is composition + submission.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.envelope_adapter import (
    to_oms_engine_envelope,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.selectors import (
    selector_for,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.trigger_ids import (
    TriggerIdGenerator,
)
from alphamind.execution.oms.engine_envelope import (
    EngineEnvelope as OmsEngineEnvelope,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionsPositionDetails,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    BreachDetails,
    CascadeContext,
    EngineEnvelope,
    EvaluateProposalsCallable,
    LibraryConfigProtocol,
    MarginCallEvent,
    MarketInputsProtocol,
    PortfolioStateSnapshotProtocol,
    PositionLiquidity,
    PositionRiskReward,
    PositionSelectionAction,
    PositionSelectionResult,
    ProgressiveTier,
    ProposedClose,
    RegimeLabel,
    SecondaryBreachOutcome,
    generate_cascade_id,
    orchestrate_breach_cascade,
    orchestrate_margin_call_cascade,
)
from alphamind.risk_guardrails.breach_behavior.cascade import (
    FollowUpBreachSelectorProtocol,
)

if TYPE_CHECKING:
    # ``SubmissionResult`` lives in ``submit_envelope_mcp``, which depends on
    # ``decision.portfolio_manager`` — eager import would trigger the OMS ↔
    # PM circular import. The dispatcher only annotates the submit callable's
    # return type; the runtime never materialises the symbol from this
    # module's import path.
    from alphamind.execution.oms.submit_envelope_mcp import SubmissionResult


# ---------------------------------------------------------------------------
# Public value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreachDispatchContext:
    """Per-tick context the dispatcher needs to compose envelopes.

    Built fresh by the caller per ``handle_immediate_breach`` invocation so
    every secondary-breach check runs against the latest snapshot. The
    provider's read-time semantics are the caller's concern; the dispatcher
    treats the returned context as an immutable value.

    ``breach_classification`` and ``progressive_tiers`` are optional cascade
    follow-up inputs: provide them to enable post-execution re-evaluation that
    surfaces newly-FAIL'd immediate-engine rules. Leave empty / default to
    short-circuit the cascade loop at envelope #1 (the contract this story
    targets).

    Dataclass-frozen (not Pydantic) because the Protocol-typed fields
    (``library_snapshot``, ``library_config``, ``market_inputs``,
    ``evaluate_proposals``) are not isinstance-checkable and Pydantic's
    arbitrary-types-allowed path would still reject them.
    """

    open_positions: tuple[PositionView, ...]
    liquidity: tuple[PositionLiquidity, ...]
    risk_reward_metric: tuple[PositionRiskReward, ...]
    library_snapshot: PortfolioStateSnapshotProtocol
    library_config: LibraryConfigProtocol
    market_inputs: MarketInputsProtocol
    evaluate_proposals: EvaluateProposalsCallable
    portfolio_value_usd: float
    active_regime: RegimeLabel
    progressive_tiers: tuple[ProgressiveTier, ...] = ()
    breach_classification: Mapping[str, BreachResponse] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeferralEvent:
    """Emitted when a protective close defers to the PM rather than submitting.

    Carries the rule the dispatcher attempted to cure, the candidate position
    the secondary-breach check rejected, the trigger id reserved for the
    rejected envelope, and the optional cascade id (when the deferral happens
    inside an in-flight cascade).
    """

    rule_breached: str
    candidate_position_id: str
    trigger_id: int
    monitor_session_id: str
    cascade_id: str | None
    reason: str
    at: datetime


# ---------------------------------------------------------------------------
# Typed callable aliases
# ---------------------------------------------------------------------------


BreachDispatchContextProvider = Callable[[], BreachDispatchContext]
SubmitEngineEnvelope = Callable[[OmsEngineEnvelope], Awaitable["SubmissionResult"]]
DeferralSink = Callable[[DeferralEvent], Awaitable[None]]
PerRuleKwargsProvider = Callable[[RuleEvaluation, BreachDispatchContext], dict[str, Any]]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class CascadeDispatcher:
    """Compose engine envelopes from breach signals and route them to the OMS path.

    The dispatcher is constructed once per monitor session and reused across
    every immediate-breach event. ``trigger_ids`` is shared across all
    dispatch paths (breach loop + margin-call handler) so the per-session
    trigger sequence stays monotonic across the union of envelope sources.
    """

    def __init__(  # noqa: PLR0913 — composition root for every per-tick dependency
        self,
        *,
        monitor_session_id: str,
        breach_config: BreachBehaviorConfig,
        trigger_ids: TriggerIdGenerator,
        context_provider: BreachDispatchContextProvider,
        submit_envelope: SubmitEngineEnvelope,
        deferral_sink: DeferralSink,
        per_rule_kwargs_providers: Mapping[str, PerRuleKwargsProvider],
        follow_up_selector: FollowUpBreachSelectorProtocol | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not monitor_session_id:
            msg = "monitor_session_id must be a non-empty string"
            raise ValueError(msg)
        if trigger_ids.session_id != monitor_session_id:
            msg = (
                f"trigger_ids.session_id {trigger_ids.session_id!r} does not match "
                f"monitor_session_id {monitor_session_id!r}"
            )
            raise ValueError(msg)
        self._monitor_session_id = monitor_session_id
        self._breach_config = breach_config
        self._trigger_ids = trigger_ids
        self._context_provider = context_provider
        self._submit_envelope = submit_envelope
        self._deferral_sink = deferral_sink
        self._per_rule_kwargs_providers = per_rule_kwargs_providers
        self._follow_up_selector = follow_up_selector
        self._now = now

    # ------------------------------------------------------------------
    # Breach-loop callback
    # ------------------------------------------------------------------

    async def handle_immediate_breach(self, result: BreachLoopResult, rule: RuleEvaluation) -> None:
        """Handle one immediate-action ``HARD_BLOCK`` breach from the breach loop.

        Resolves the per-rule selector, builds the primary close, routes
        through :func:`orchestrate_breach_cascade`, and submits each returned
        envelope. Deferred-to-PM envelopes route to the deferral sink rather
        than the OMS submit path (avoiding a deterministic rejection there).
        """
        del result  # used as positional carrier — fields read are passed via context
        selector = selector_for(rule.rule_id)
        if selector is None:
            msg = (
                f"cascade dispatcher received rule_id {rule.rule_id!r} with no "
                f"registered selector — this is a structural error; rule is either "
                f"deferred-classification or unknown"
            )
            raise ValueError(msg)
        if rule.rule_id not in self._per_rule_kwargs_providers:
            msg = (
                f"no per-rule kwargs provider registered for rule_id "
                f"{rule.rule_id!r}; the dispatcher cannot construct the "
                f"selector's arguments"
            )
            raise ValueError(msg)
        context = self._context_provider()
        kwargs = self._per_rule_kwargs_providers[rule.rule_id](rule, context)
        selection = selector(**kwargs)
        primary_close = _proposed_close_from_selection(
            selection=selection,
            position=_position_by_id(context.open_positions, selection.position_id),
            portfolio_value_usd=context.portfolio_value_usd,
        )
        initial_trigger = self._trigger_ids.next()
        cascade_id = generate_cascade_id(
            monitor_session_id=self._monitor_session_id,
            initial_trigger_id=initial_trigger,
        )
        classification, follow_up = self._cascade_follow_up_pair(context)
        envelopes = orchestrate_breach_cascade(
            primary_rule=rule.rule_id,
            primary_breach_details=_breach_details_from_evaluation(
                rule, active_regime=context.active_regime
            ),
            proposed_close=primary_close,
            primary_position_selection=selection,
            open_positions=context.open_positions,
            liquidity=context.liquidity,
            current_state=context.library_snapshot,
            library_config=context.library_config,
            market_inputs=context.market_inputs,
            active_regime=context.active_regime,
            context=CascadeContext(
                monitor_session_id=self._monitor_session_id,
                initial_trigger_id=initial_trigger,
                cascade_id=cascade_id,
                trigger_timestamp=self._now(),
                portfolio_value_usd=context.portfolio_value_usd,
                config=self._breach_config,
            ),
            evaluate_proposals=context.evaluate_proposals,
            breach_classification=classification,
            follow_up_selector=follow_up,
        )
        await self._submit_or_defer_envelopes(
            envelopes,
            rule_breached=rule.rule_id,
            cascade_id=cascade_id,
            initial_trigger=initial_trigger,
        )

    # ------------------------------------------------------------------
    # Margin-call entry — broker-fill / account-state path
    # ------------------------------------------------------------------

    async def handle_margin_call(self, event: MarginCallEvent) -> None:
        """Handle a broker-issued margin call.

        Routes through :func:`orchestrate_margin_call_cascade` to liquidate
        the worst-R/R position and chain any post-liquidation follow-up
        envelopes under a shared ``cascade_id``.
        """
        context = self._context_provider()
        initial_trigger = self._trigger_ids.next()
        cascade_id = generate_cascade_id(
            monitor_session_id=self._monitor_session_id,
            initial_trigger_id=initial_trigger,
        )
        classification, follow_up = self._cascade_follow_up_pair(context)
        envelopes = orchestrate_margin_call_cascade(
            margin_call_event=event,
            open_positions=context.open_positions,
            liquidity=context.liquidity,
            risk_reward_metric=context.risk_reward_metric,
            current_state=context.library_snapshot,
            library_config=context.library_config,
            market_inputs=context.market_inputs,
            active_regime=context.active_regime,
            context=CascadeContext(
                monitor_session_id=self._monitor_session_id,
                initial_trigger_id=initial_trigger,
                cascade_id=cascade_id,
                trigger_timestamp=self._now(),
                portfolio_value_usd=context.portfolio_value_usd,
                config=self._breach_config,
            ),
            evaluate_proposals=context.evaluate_proposals,
            breach_classification=classification,
            follow_up_selector=follow_up,
        )
        await self._submit_or_defer_envelopes(
            envelopes,
            rule_breached="margin_call",
            cascade_id=cascade_id,
            initial_trigger=initial_trigger,
        )

    # ------------------------------------------------------------------
    # Internal — cascade follow-up pair + submit / defer routing
    # ------------------------------------------------------------------

    def _cascade_follow_up_pair(
        self, context: BreachDispatchContext
    ) -> tuple[Mapping[str, BreachResponse] | None, FollowUpBreachSelectorProtocol | None]:
        """Return the ``(classification, follow_up_selector)`` pair the cascade
        orchestrator expects.

        Both must be provided or both ``None`` — see
        ``cascade._check_classification_selector_pair``. We tighten that here:
        require *both* a non-empty classification map *and* an injected
        follow-up selector before enabling cascade follow-ups; otherwise the
        cascade short-circuits at envelope #1.
        """
        if context.breach_classification and self._follow_up_selector is not None:
            return context.breach_classification, self._follow_up_selector
        return None, None

    async def _submit_or_defer_envelopes(
        self,
        envelopes: tuple[EngineEnvelope, ...],
        *,
        rule_breached: str,
        cascade_id: str,
        initial_trigger: int,
    ) -> None:
        for ordinal, envelope in enumerate(envelopes):
            sbcr = envelope.guardrail_trigger_record.secondary_breach_check_result
            if sbcr is not None and sbcr.result == SecondaryBreachOutcome.DEFERRED_TO_PM:
                await self._deferral_sink(
                    DeferralEvent(
                        rule_breached=rule_breached,
                        candidate_position_id=envelope.command.position_id,
                        trigger_id=initial_trigger + ordinal,
                        monitor_session_id=self._monitor_session_id,
                        cascade_id=cascade_id,
                        reason=sbcr.notes or "secondary breach not cleared",
                        at=self._now(),
                    )
                )
                continue
            await self._submit_envelope(to_oms_engine_envelope(envelope))


# ---------------------------------------------------------------------------
# Helpers — projection from selection result to proposed close
# ---------------------------------------------------------------------------


def _position_by_id(positions: tuple[PositionView, ...], position_id: str) -> PositionView:
    for position in positions:
        if position.position_id == position_id:
            return position
    msg = f"position_id {position_id!r} not present in open_positions"
    raise ValueError(msg)


def _ticker_of(position: PositionView) -> str:
    """Return a human-readable ticker label for the rationale string.

    Mirrors :func:`alphamind.risk_guardrails.breach_behavior.position_selection._ticker_of`;
    inlined here to avoid reaching for a private symbol.
    """
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    if isinstance(details, StrategyPositionDetails):
        return details.strategy_type_label
    return position.position_id


def _proposed_close_from_selection(
    *,
    selection: PositionSelectionResult,
    position: PositionView,
    portfolio_value_usd: float,
) -> ProposedClose:
    """Translate a position-selection result + record into a ProposedClose.

    Mirrors the breach-behavior internal ``_proposed_close_from_selection`` in
    :mod:`alphamind.risk_guardrails.breach_behavior.cascade` — we re-derive it
    here so the dispatcher does not depend on a private symbol.
    """
    pre_pct = abs(position.position_weight_pct)
    pre_usd = pre_pct / 100.0 * portfolio_value_usd
    if selection.action == PositionSelectionAction.FULL_CLOSE:
        close_pct = pre_pct
        close_usd = pre_usd
    else:
        target_pct = selection.target_post_action_size_pct_of_portfolio
        if target_pct is None:
            msg = "PARTIAL_TRIM action requires target_post_action_size_pct_of_portfolio"
            raise ValueError(msg)
        close_pct = max(pre_pct - target_pct, 0.0)
        close_usd = close_pct / 100.0 * portfolio_value_usd

    asset_type: Literal["equity", "option", "strategy"]
    if position.instrument_type == InstrumentType.EQUITY:
        asset_type = "equity"
    elif position.instrument_type == InstrumentType.OPTIONS:
        asset_type = "option"
    else:
        asset_type = "strategy"
    direction: Literal["long", "short"] = (
        "long" if position.direction == Direction.LONG else "short"
    )
    return ProposedClose(
        position_id=position.position_id,
        ticker=_ticker_of(position),
        asset_type=asset_type,
        direction=direction,
        pre_close_size_pct_of_portfolio=pre_pct,
        close_size_pct_of_portfolio=close_pct,
        pre_close_size_usd=pre_usd,
        close_size_usd=close_usd,
    )


def _breach_details_from_evaluation(
    rule: RuleEvaluation,
    *,
    active_regime: RegimeLabel,
) -> BreachDetails:
    """Project a :class:`RuleEvaluation` into the envelope's :class:`BreachDetails`."""
    return BreachDetails(
        current_value=rule.current_value,
        limit_value=rule.limit_value,
        overage=max(rule.overage, 0.0),
        unit=None,
        regime_at_breach=active_regime,
    )


__all__ = [
    "BreachDispatchContext",
    "BreachDispatchContextProvider",
    "CascadeDispatcher",
    "DeferralEvent",
    "DeferralSink",
    "PerRuleKwargsProvider",
    "SubmitEngineEnvelope",
]
