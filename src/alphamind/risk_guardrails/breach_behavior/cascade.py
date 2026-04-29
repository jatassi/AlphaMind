"""Cascade orchestration for margin call and primary→secondary breach chains (story 07).

Two orchestrator entry points:

* :func:`orchestrate_margin_call_cascade` — handles the broker-issued margin
  call with absolute priority. Selects the worst-R/R position for full close,
  records any secondary breach as ``DEFERRED_TO_PM`` (margin calls never
  alternate-search), and chains follow-up envelopes for any *new* immediate-
  engine breaches surfaced by post-liquidation re-evaluation.
* :func:`orchestrate_breach_cascade` — handles the non-margin primary→secondary
  cascade. On secondary breach, attempts an alternate-position search; the
  first clean alternate wins, otherwise the original close is emitted with
  ``DEFERRED_TO_PM``.

Both orchestrators emit one envelope per cascade step via
:func:`compose_engine_envelope` (story 06); each envelope shares the cascade's
``cascade_id`` and uses a distinct, monotonic ``trigger_id`` starting at
``context.initial_trigger_id``. Per
``docs/design/06-risk-guardrails/breach-behavior.md`` § *Margin call cascade
handling* and § *Secondary breach checking*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from alphamind.config.models.guardrails import BreachResponse
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    PositionRecord,
)
from alphamind.risk_guardrails.breach_behavior.config import BreachBehaviorConfig
from alphamind.risk_guardrails.breach_behavior.emergency_triggers import MarginCallEvent
from alphamind.risk_guardrails.breach_behavior.engine_envelope import compose_engine_envelope
from alphamind.risk_guardrails.breach_behavior.hard_rejection import RuleProjectionProtocol
from alphamind.risk_guardrails.breach_behavior.position_selection import (
    PositionLiquidity,
    PositionRiskReward,
    select_for_margin_call,
)
from alphamind.risk_guardrails.breach_behavior.secondary_breach import (
    EvaluateProposalsCallable,
    LibraryConfigProtocol,
    MarketInputsProtocol,
    PortfolioStateSnapshotProtocol,
    ProposedClose,
    check_secondary_breach,
)
from alphamind.risk_guardrails.breach_behavior.types import (
    BreachDetails,
    EngineEnvelope,
    PositionSelectionAction,
    PositionSelectionResult,
    RegimeLabel,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
)

_FAIL_STATUS = "FAIL"
_MARGIN_CALL_RULE_ID = "margin_call"
_AssetType = Literal["equity", "option", "strategy"]
_DirectionLabel = Literal["long", "short"]
_CandidateBreachType = Literal["sector", "directional", "other"]


def generate_cascade_id(*, monitor_session_id: str, initial_trigger_id: int) -> str:
    """Return a deterministic cascade ID derived from the initiating trigger.

    Format: ``"CASCADE.{monitor_session_id}.{initial_trigger_id}"``. Unique
    within a monitor session because trigger IDs are monotonic; a new session
    produces a fresh prefix.

    Raises:
        ValueError: when ``monitor_session_id`` is empty or contains ``.``, or
            when ``initial_trigger_id < 1``.
    """
    if not monitor_session_id:
        msg = "monitor_session_id must be a non-empty string"
        raise ValueError(msg)
    if "." in monitor_session_id:
        msg = (
            f"monitor_session_id must not contain '.' (the field separator); "
            f"got {monitor_session_id!r}"
        )
        raise ValueError(msg)
    if initial_trigger_id < 1:
        msg = f"initial_trigger_id must be >= 1; got {initial_trigger_id}"
        raise ValueError(msg)
    return f"CASCADE.{monitor_session_id}.{initial_trigger_id}"


# ---------------------------------------------------------------------------
# CascadeContext / CascadeStepLimitExceeded
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CascadeContext:
    """Inputs threaded through cascade orchestration."""

    monitor_session_id: str
    initial_trigger_id: int
    cascade_id: str
    trigger_timestamp: datetime
    portfolio_value_usd: float
    config: BreachBehaviorConfig


class CascadeStepLimitExceeded(Exception):  # noqa: N818 — public name mandated by story 07
    """Raised when a cascade chain reaches ``config.cascade_max_steps``."""

    def __init__(
        self,
        chain_length: int,
        max_steps: int,
        last_breach_rule: str | None,
    ) -> None:
        self.chain_length = chain_length
        self.max_steps = max_steps
        self.last_breach_rule = last_breach_rule
        msg = (
            f"Cascade chain length {chain_length} exceeded max_steps {max_steps}; "
            f"last breach was {last_breach_rule!r}"
        )
        super().__init__(msg)


class FollowUpBreachSelectorProtocol(Protocol):
    """Selects a follow-up close for a post-liquidation immediate-engine breach.

    The continuous monitor dispatches to the appropriate per-rule selector from
    story 04d (drawdown, position-max-loss, total-short, single-short) based on
    ``rule_id``. Returns the resulting :class:`PositionSelectionResult` and the
    :class:`ProposedClose` constructed from it for the secondary-breach check.
    """

    def __call__(
        self,
        *,
        rule_id: str,
        rule_projection: RuleProjectionProtocol,
        post_liquidation_positions: tuple[PositionRecord, ...],
        liquidity: tuple[PositionLiquidity, ...],
        portfolio_value_usd: float,
    ) -> tuple[PositionSelectionResult, ProposedClose]: ...


# ---------------------------------------------------------------------------
# Helpers — translate PositionRecord/PositionSelectionResult into ProposedClose
# ---------------------------------------------------------------------------


def _ticker_of(position: PositionRecord) -> str:
    if isinstance(position.equity_details, EquityPositionDetails):
        return position.equity_details.ticker
    if position.options_details is not None:
        return position.options_details.underlying_ticker
    if position.strategy_details is not None:
        return position.strategy_details.strategy_type_label
    return position.position_id


def _asset_type_of(position: PositionRecord) -> _AssetType:
    if position.instrument_type == InstrumentType.EQUITY:
        return "equity"
    if position.instrument_type == InstrumentType.OPTIONS:
        return "option"
    return "strategy"


def _direction_of(position: PositionRecord) -> _DirectionLabel:
    return "long" if position.direction == Direction.LONG else "short"


def _proposed_close_from_selection(
    *,
    selection: PositionSelectionResult,
    position: PositionRecord,
    portfolio_value_usd: float,
) -> ProposedClose:
    """Translate a position-selection result + record into a ProposedClose."""
    pre_pct = position.position_weight_pct
    pre_usd = pre_pct / 100.0 * portfolio_value_usd
    if selection.action == PositionSelectionAction.FULL_CLOSE:
        close_pct = pre_pct
        close_usd = pre_usd
    else:
        target_pct = selection.target_post_action_size_pct_of_portfolio
        # PARTIAL_TRIM invariant: target_pct is non-None.
        assert target_pct is not None
        close_pct = max(pre_pct - target_pct, 0.0)
        close_usd = close_pct / 100.0 * portfolio_value_usd
    return ProposedClose(
        position_id=position.position_id,
        ticker=_ticker_of(position),
        asset_type=_asset_type_of(position),
        direction=_direction_of(position),
        pre_close_size_pct_of_portfolio=pre_pct,
        close_size_pct_of_portfolio=close_pct,
        pre_close_size_usd=pre_usd,
        close_size_usd=close_usd,
    )


def _full_close_proposed_close(
    position: PositionRecord, *, portfolio_value_usd: float
) -> ProposedClose:
    """Build a full-close ProposedClose for the alternate-position search."""
    pre_pct = position.position_weight_pct
    pre_usd = pre_pct / 100.0 * portfolio_value_usd
    return ProposedClose(
        position_id=position.position_id,
        ticker=_ticker_of(position),
        asset_type=_asset_type_of(position),
        direction=_direction_of(position),
        pre_close_size_pct_of_portfolio=pre_pct,
        close_size_pct_of_portfolio=pre_pct,
        pre_close_size_usd=pre_usd,
        close_size_usd=pre_usd,
    )


def _breach_details_from_projection(
    rule_projection: RuleProjectionProtocol,
    *,
    active_regime: RegimeLabel | None = None,
) -> BreachDetails:
    """Project a per-rule projection into the envelope's BreachDetails."""
    return BreachDetails(
        current_value=rule_projection.current,
        limit_value=rule_projection.limit,
        overage=max(0.0, rule_projection.projected_after - rule_projection.limit),
        unit=rule_projection.unit,
        regime_at_breach=active_regime,
    )


def _margin_call_breach_details(
    margin_call_event_amount_usd: float,
    *,
    active_regime: RegimeLabel | None = None,
) -> BreachDetails:
    """Construct BreachDetails for a margin call (no library projection drives this)."""
    return BreachDetails(
        current_value=margin_call_event_amount_usd,
        limit_value=0.0,
        overage=margin_call_event_amount_usd,
        unit="usd",
        regime_at_breach=active_regime,
    )


def _validate_inputs(
    *,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
) -> None:
    if not open_positions:
        msg = "open_positions must be non-empty"
        raise ValueError(msg)
    liquidity_ids = {liq.position_id for liq in liquidity}
    missing = [p.position_id for p in open_positions if p.position_id not in liquidity_ids]
    if missing:
        msg = f"liquidity missing entries for position_ids: {missing}"
        raise ValueError(msg)


def _newly_failed_immediate_rules(
    *,
    baseline: tuple[RuleProjectionProtocol, ...],
    post_close: tuple[RuleProjectionProtocol, ...],
    excluded_rule_ids: frozenset[str],
    breach_classification: Mapping[str, BreachResponse],
) -> tuple[RuleProjectionProtocol, ...]:
    """Return post-close FAIL'd rules absent from baseline FAILs.

    Excludes the rule(s) the close was meant to cure. Only returns rules whose
    ``BreachResponse`` is ``immediate_engine`` — ``deferred_to_pm`` breaches
    surface to the next invocation, not via additional cascade envelopes.
    """
    baseline_status = {r.rule: r.status for r in baseline}
    follow_ups: list[RuleProjectionProtocol] = []
    for rule_proj in post_close:
        if rule_proj.rule in excluded_rule_ids:
            continue
        if rule_proj.status != _FAIL_STATUS:
            continue
        if baseline_status.get(rule_proj.rule) == _FAIL_STATUS:
            continue
        if breach_classification.get(rule_proj.rule) != BreachResponse.immediate_engine:
            continue
        follow_ups.append(rule_proj)
    return tuple(follow_ups)


# ---------------------------------------------------------------------------
# Margin call cascade orchestrator
# ---------------------------------------------------------------------------


def orchestrate_margin_call_cascade(  # noqa: PLR0913 — orchestrator surface mandated by story 07
    *,
    margin_call_event: MarginCallEvent,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    risk_reward_metric: tuple[PositionRiskReward, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    active_regime: RegimeLabel,
    context: CascadeContext,
    evaluate_proposals: EvaluateProposalsCallable,
    breach_classification: Mapping[str, BreachResponse] | None = None,
    follow_up_selector: FollowUpBreachSelectorProtocol | None = None,
) -> tuple[EngineEnvelope, ...]:
    """Orchestrate the multi-step margin call cascade.

    Sequence:
      1. Use :func:`select_for_margin_call` to pick the worst-R/R position.
      2. Project the close via :func:`check_secondary_breach`. Margin calls
         *never* alternate-search; any introduced secondary surfaces as
         ``DEFERRED_TO_PM`` on the initial envelope's
         ``secondary_breach_check_result``.
      3. Compose envelope #1 via :func:`compose_engine_envelope` with
         ``cascade_id = context.cascade_id`` and ``rule_breached="margin_call"``.
      4. Re-evaluate post-liquidation: identify newly-FAIL'd rules whose
         classification is ``immediate_engine``. For each, dispatch via
         ``follow_up_selector`` and emit envelope #N+1 with the same
         ``cascade_id`` and a monotonic ``trigger_id``.
      5. Stop when no new immediate-engine breaches surface or
         ``context.config.cascade_max_steps`` is reached. Hitting the cap
         raises :class:`CascadeStepLimitExceeded`.

    ``follow_up_selector`` is invoked only when post-liquidation re-evaluation
    surfaces an immediate-engine breach distinct from the closed rule. For the
    A7 reproduction (no follow-up breaches), ``follow_up_selector`` may be
    ``None``.

    Returns:
        Tuple of :class:`EngineEnvelope`, in cascade order. The first envelope
        is the margin-call liquidation; subsequent envelopes are post-
        liquidation forced reductions.

    Raises:
        ValueError: on input-validation failures.
        CascadeStepLimitExceeded: when the cascade reaches
            ``context.config.cascade_max_steps``.
    """
    _validate_inputs(open_positions=open_positions, liquidity=liquidity)

    margin_amount = margin_call_event.additional_margin_required_usd
    selection = select_for_margin_call(
        open_positions=open_positions,
        liquidity=liquidity,
        additional_margin_required_usd=margin_amount,
        risk_reward_metric=risk_reward_metric,
    )
    positions_by_id = {p.position_id: p for p in open_positions}
    selected_position = positions_by_id[selection.position_id]
    proposed_close = _proposed_close_from_selection(
        selection=selection,
        position=selected_position,
        portfolio_value_usd=context.portfolio_value_usd,
    )

    secondary = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=current_state,
        library_config=library_config,
        market_inputs=market_inputs,
        primary_breach_rule_id=_MARGIN_CALL_RULE_ID,
        config=context.config,
        evaluate_proposals=evaluate_proposals,
    )

    envelopes: list[EngineEnvelope] = [
        compose_engine_envelope(
            monitor_session_id=context.monitor_session_id,
            trigger_id=context.initial_trigger_id,
            trigger_timestamp=context.trigger_timestamp,
            rule_breached=_MARGIN_CALL_RULE_ID,
            breach_details=_margin_call_breach_details(margin_amount, active_regime=active_regime),
            position_selection=selection,
            positions_by_id=positions_by_id,
            portfolio_value_usd=context.portfolio_value_usd,
            cascade_id=context.cascade_id,
            secondary_breach_check=secondary,
        ),
    ]

    if breach_classification is None or follow_up_selector is None:
        return tuple(envelopes)
    _extend_with_follow_ups(
        envelopes=envelopes,
        initial_selection=selection,
        initial_excluded_rule=_MARGIN_CALL_RULE_ID,
        open_positions=open_positions,
        liquidity=liquidity,
        current_state=current_state,
        library_config=library_config,
        market_inputs=market_inputs,
        active_regime=active_regime,
        context=context,
        evaluate_proposals=evaluate_proposals,
        breach_classification=breach_classification,
        follow_up_selector=follow_up_selector,
    )
    return tuple(envelopes)


def _apply_close_to_positions(
    open_positions: tuple[PositionRecord, ...],
    selection: PositionSelectionResult,
) -> tuple[PositionRecord, ...]:
    """Hypothetical post-close position set; full-close removes, partial-trim retains.

    Margin calls always full-close, so the post-liquidation set excludes the
    selected position. Partial trims retain the position (size adjustment is
    not modeled at the record level here).
    """
    if selection.action == PositionSelectionAction.FULL_CLOSE:
        return tuple(p for p in open_positions if p.position_id != selection.position_id)
    return open_positions


def _extend_with_follow_ups(  # noqa: PLR0913 — internal helper threading cascade-loop state
    *,
    envelopes: list[EngineEnvelope],
    initial_selection: PositionSelectionResult,
    initial_excluded_rule: str,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    active_regime: RegimeLabel,
    context: CascadeContext,
    evaluate_proposals: EvaluateProposalsCallable,
    breach_classification: Mapping[str, BreachResponse],
    follow_up_selector: FollowUpBreachSelectorProtocol,
) -> None:
    """Walk the post-liquidation cascade; append immediate-engine follow-ups."""
    max_steps = context.config.cascade_max_steps
    excluded: set[str] = {initial_excluded_rule}
    current_positions = _apply_close_to_positions(open_positions, initial_selection)
    last_breach_rule = initial_excluded_rule
    pre_cascade_baseline = evaluate_proposals(
        state=current_state, proposals=(), config=library_config, market=market_inputs
    )

    while True:
        post_state = evaluate_proposals(
            state=current_state, proposals=(), config=library_config, market=market_inputs
        )
        new_breaches = _newly_failed_immediate_rules(
            baseline=pre_cascade_baseline.per_rule,
            post_close=post_state.per_rule,
            excluded_rule_ids=frozenset(excluded),
            breach_classification=breach_classification,
        )
        if not new_breaches:
            return

        if len(envelopes) >= max_steps:
            raise CascadeStepLimitExceeded(
                chain_length=len(envelopes) + 1,
                max_steps=max_steps,
                last_breach_rule=last_breach_rule,
            )

        next_breach = new_breaches[0]
        last_breach_rule = next_breach.rule
        excluded.add(next_breach.rule)

        follow_selection, follow_close = follow_up_selector(
            rule_id=next_breach.rule,
            rule_projection=next_breach,
            post_liquidation_positions=current_positions,
            liquidity=liquidity,
            portfolio_value_usd=context.portfolio_value_usd,
        )
        follow_secondary = check_secondary_breach(
            proposed_close=follow_close,
            current_state=current_state,
            library_config=library_config,
            market_inputs=market_inputs,
            primary_breach_rule_id=next_breach.rule,
            config=context.config,
            evaluate_proposals=evaluate_proposals,
        )
        positions_by_id = {p.position_id: p for p in current_positions}
        envelopes.append(
            compose_engine_envelope(
                monitor_session_id=context.monitor_session_id,
                trigger_id=context.initial_trigger_id + len(envelopes),
                trigger_timestamp=context.trigger_timestamp,
                rule_breached=next_breach.rule,
                breach_details=_breach_details_from_projection(
                    next_breach, active_regime=active_regime
                ),
                position_selection=follow_selection,
                positions_by_id=positions_by_id,
                portfolio_value_usd=context.portfolio_value_usd,
                cascade_id=context.cascade_id,
                secondary_breach_check=follow_secondary,
            )
        )
        current_positions = _apply_close_to_positions(current_positions, follow_selection)


# ---------------------------------------------------------------------------
# Non-margin breach cascade orchestrator
# ---------------------------------------------------------------------------


def orchestrate_breach_cascade(  # noqa: PLR0913 — orchestrator surface mandated by story 07
    *,
    primary_rule: str,
    primary_breach_details: BreachDetails,
    proposed_close: ProposedClose,
    primary_position_selection: PositionSelectionResult,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    active_regime: RegimeLabel,
    context: CascadeContext,
    evaluate_proposals: EvaluateProposalsCallable,
    primary_rule_breach_type: _CandidateBreachType = "other",
    breach_classification: Mapping[str, BreachResponse] | None = None,
    follow_up_selector: FollowUpBreachSelectorProtocol | None = None,
) -> tuple[EngineEnvelope, ...]:
    """Orchestrate a non-margin primary→secondary breach cascade.

    Sequence:
      1. Run :func:`check_secondary_breach` on ``proposed_close``.
      2. ``NO_SECONDARY_BREACH`` → emit one envelope on the original close.
      3. ``DEFERRED_TO_PM`` → :func:`search_for_alternate_position`:
         - Clean alternate found: emit one envelope on the alternate with
           ``SECONDARY_BREACH_AVOIDED`` and notes describing the swap.
         - No clean alternate: emit one envelope on the original close with
           ``DEFERRED_TO_PM`` and notes naming the secondary.
      4. Post-execution re-evaluation chains immediate-engine follow-ups (same
         logic as :func:`orchestrate_margin_call_cascade`).

    ``primary_rule_breach_type`` restricts the alternate-position-search
    candidate set: ``"sector"`` filters to same-sector positions, ``"directional"``
    filters by long/short side, ``"other"`` (default) considers all positions.
    """
    _validate_inputs(open_positions=open_positions, liquidity=liquidity)

    initial_secondary = check_secondary_breach(
        proposed_close=proposed_close,
        current_state=current_state,
        library_config=library_config,
        market_inputs=market_inputs,
        primary_breach_rule_id=primary_rule,
        config=context.config,
        evaluate_proposals=evaluate_proposals,
    )

    final_selection, final_secondary = _resolve_primary_or_alternate(
        primary_rule=primary_rule,
        primary_position_selection=primary_position_selection,
        initial_secondary=initial_secondary,
        open_positions=open_positions,
        liquidity=liquidity,
        current_state=current_state,
        library_config=library_config,
        market_inputs=market_inputs,
        evaluate_proposals=evaluate_proposals,
        context=context,
        primary_rule_breach_type=primary_rule_breach_type,
    )

    positions_by_id = {p.position_id: p for p in open_positions}
    envelopes: list[EngineEnvelope] = [
        compose_engine_envelope(
            monitor_session_id=context.monitor_session_id,
            trigger_id=context.initial_trigger_id,
            trigger_timestamp=context.trigger_timestamp,
            rule_breached=primary_rule,
            breach_details=primary_breach_details,
            position_selection=final_selection,
            positions_by_id=positions_by_id,
            portfolio_value_usd=context.portfolio_value_usd,
            cascade_id=context.cascade_id,
            secondary_breach_check=final_secondary,
        ),
    ]

    if breach_classification is None or follow_up_selector is None:
        return tuple(envelopes)
    _extend_with_follow_ups(
        envelopes=envelopes,
        initial_selection=final_selection,
        initial_excluded_rule=primary_rule,
        open_positions=open_positions,
        liquidity=liquidity,
        current_state=current_state,
        library_config=library_config,
        market_inputs=market_inputs,
        active_regime=active_regime,
        context=context,
        evaluate_proposals=evaluate_proposals,
        breach_classification=breach_classification,
        follow_up_selector=follow_up_selector,
    )
    return tuple(envelopes)


def _resolve_primary_or_alternate(  # noqa: PLR0913 — orchestrator-internal swap helper
    *,
    primary_rule: str,
    primary_position_selection: PositionSelectionResult,
    initial_secondary: SecondaryBreachCheckResult,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    evaluate_proposals: EvaluateProposalsCallable,
    context: CascadeContext,
    primary_rule_breach_type: _CandidateBreachType,
) -> tuple[PositionSelectionResult, SecondaryBreachCheckResult]:
    """Return the (selection, secondary-check-result) pair to embed in envelope #1."""
    if initial_secondary.result != SecondaryBreachOutcome.DEFERRED_TO_PM:
        return primary_position_selection, initial_secondary

    alternate = search_for_alternate_position(
        primary_rule=primary_rule,
        primary_position_selection=primary_position_selection,
        open_positions=open_positions,
        liquidity=liquidity,
        current_state=current_state,
        library_config=library_config,
        market_inputs=market_inputs,
        config=context.config,
        evaluate_proposals=evaluate_proposals,
        portfolio_value_usd=context.portfolio_value_usd,
        primary_rule_breach_type=primary_rule_breach_type,
    )
    if alternate is None:
        return primary_position_selection, SecondaryBreachCheckResult(
            result=SecondaryBreachOutcome.DEFERRED_TO_PM,
            notes=(
                f"no clean alternate found; original close on "
                f"{primary_position_selection.position_id} executed despite "
                f"secondary breach: {initial_secondary.notes or 'unspecified'}"
            ),
        )
    alt_selection, _alt_close = alternate
    return alt_selection, SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.SECONDARY_BREACH_AVOIDED,
        notes=(
            f"alternate {alt_selection.position_id} clears {primary_rule}; "
            f"original {primary_position_selection.position_id} would have "
            f"introduced: {initial_secondary.notes or 'secondary breach'}"
        ),
    )


# ---------------------------------------------------------------------------
# Alternate-position search
# ---------------------------------------------------------------------------


def _candidate_set(
    *,
    primary_position_id: str,
    open_positions: tuple[PositionRecord, ...],
    primary_rule_breach_type: _CandidateBreachType,
) -> tuple[PositionRecord, ...]:
    """Restrict the alternate-search candidate set by breach type.

    ``"sector"`` — same-sector positions only (caller responsibility: tag
    positions or filter upstream; this implementation falls back to all
    positions in absence of sector metadata).
    ``"directional"`` — long if the primary was long; short if short.
    ``"other"`` — full set excluding the original.
    """
    candidates = tuple(p for p in open_positions if p.position_id != primary_position_id)
    if primary_rule_breach_type == "directional":
        primary = next(
            (p for p in open_positions if p.position_id == primary_position_id),
            None,
        )
        if primary is not None:
            return tuple(c for c in candidates if c.direction == primary.direction)
    return candidates


def search_for_alternate_position(  # noqa: PLR0913 — search surface mandated by story 07
    *,
    primary_rule: str,
    primary_position_selection: PositionSelectionResult,
    open_positions: tuple[PositionRecord, ...],
    liquidity: tuple[PositionLiquidity, ...],
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    config: BreachBehaviorConfig,
    evaluate_proposals: EvaluateProposalsCallable,
    portfolio_value_usd: float,
    primary_rule_breach_type: _CandidateBreachType = "other",
) -> tuple[PositionSelectionResult, ProposedClose] | None:
    """Search for a position whose close cures the primary without secondary breach.

    Iterates a candidate set derived from ``primary_rule_breach_type`` (sector
    breaches → same-sector; directional → same-side; other → all but original),
    constructs a hypothetical full-close ProposedClose for each candidate, and
    runs :func:`check_secondary_breach`. Returns the first
    ``NO_SECONDARY_BREACH`` candidate. The search is bounded by
    ``config.cascade_max_steps`` candidates.

    Returns:
        ``(PositionSelectionResult, ProposedClose)`` for the first clean
        alternate; ``None`` if none clears within the bound.
    """
    _validate_inputs(open_positions=open_positions, liquidity=liquidity)
    candidates = _candidate_set(
        primary_position_id=primary_position_selection.position_id,
        open_positions=open_positions,
        primary_rule_breach_type=primary_rule_breach_type,
    )

    for candidate in candidates[: config.cascade_max_steps]:
        alt_close = _full_close_proposed_close(candidate, portfolio_value_usd=portfolio_value_usd)
        secondary = check_secondary_breach(
            proposed_close=alt_close,
            current_state=current_state,
            library_config=library_config,
            market_inputs=market_inputs,
            primary_breach_rule_id=primary_rule,
            config=config,
            evaluate_proposals=evaluate_proposals,
        )
        if secondary.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH:
            alt_selection = PositionSelectionResult(
                position_id=candidate.position_id,
                action=PositionSelectionAction.FULL_CLOSE,
                target_post_action_size_pct_of_portfolio=None,
                rationale=(
                    f"alternate full close on {_ticker_of(candidate)} "
                    f"({candidate.position_id}) chosen over "
                    f"{primary_position_selection.position_id} to avoid secondary breach"
                ),
            )
            return alt_selection, alt_close
    return None


__all__ = [
    "CascadeContext",
    "CascadeStepLimitExceeded",
    "FollowUpBreachSelectorProtocol",
    "generate_cascade_id",
    "orchestrate_breach_cascade",
    "orchestrate_margin_call_cascade",
    "search_for_alternate_position",
]
