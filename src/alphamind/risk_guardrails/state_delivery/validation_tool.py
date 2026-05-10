"""Guardrail validation tool — agent-callable pre-submission check (story 07).

The tool composes the guardrail-evaluation library's per-rule projection
primitives, layers cumulative-impact tracking across multiple calls within a
single agent invocation, and emits the canonical typed I/O contract documented
in ``state-delivery.md`` § Guardrail validation tool.

The library owns the math (per-rule projection, delta-adjusted exposure,
Black-Scholes greeks, regime parameter resolution, feature-flag early-exit).
This module owns the typed I/O contract, the ``ValidationToolState`` cumulative
tracking, the ``cumulative_impact_note`` framing, and the ``failure_guidance``
agent-friendly action recommendations.

The function is pure: same ``(request, state)`` inputs produce identical
``ValidationResult`` outputs. The state is immutable — the caller invokes
``state.with_accepted_proposal(...)`` after a PASS to create a new state for
the next call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, SkipValidation, model_validator

from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action as LibraryAction,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    AssetType as LibraryAssetType,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType as LibraryContractType,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    Direction as LibraryDirection,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    Greeks,
    LibraryConfig,
    MarketInputs,
    OptionLeg,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleProjection,
    Status,
    evaluate_proposals,
)

# ---------------------------------------------------------------------------
# Typed I/O records
# ---------------------------------------------------------------------------


class ValidationStrategyLeg(BaseModel):
    """One leg of a multi-leg STRATEGY proposal."""

    model_config = ConfigDict(frozen=True)

    direction: Direction
    asset_type: InstrumentType
    strike: float | None = None
    expiration: datetime | None = None
    contract_type: Literal["call", "put"] | None = None
    quantity: int


class ValidationInstrument(BaseModel):
    """The instrument being validated."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    asset_type: InstrumentType
    direction: Direction
    strike: float | None = None
    expiration: datetime | None = None
    contract_type: Literal["call", "put"] | None = None
    legs: tuple[ValidationStrategyLeg, ...] | None = None

    @model_validator(mode="after")
    def _validate_options_fields(self) -> ValidationInstrument:
        if self.asset_type == InstrumentType.OPTIONS:
            missing = [
                f for f in ("strike", "expiration", "contract_type") if getattr(self, f) is None
            ]
            if missing:
                msg = f"OPTIONS asset_type requires {missing}"
                raise ValueError(msg)
        if self.asset_type == InstrumentType.STRATEGY and not self.legs:
            msg = "STRATEGY asset_type requires non-empty legs"
            raise ValueError(msg)
        return self


class ValidationSize(BaseModel):
    """Size of the proposed change."""

    model_config = ConfigDict(frozen=True)

    quantity: int
    dollar_value: float
    premium_at_risk_usd: float | None = None

    @model_validator(mode="after")
    def _validate_non_negative(self) -> ValidationSize:
        if self.dollar_value < 0:
            msg = f"dollar_value must be non-negative; got {self.dollar_value}"
            raise ValueError(msg)
        if self.premium_at_risk_usd is not None and self.premium_at_risk_usd < 0:
            msg = (
                f"premium_at_risk_usd must be non-negative when set; got {self.premium_at_risk_usd}"
            )
            raise ValueError(msg)
        return self


class ValidationAction(StrEnum):
    """Action subset accepted by the validation tool.

    Deliberate subset of ``guardrail_evaluation.Action`` (which also has
    ``CANCEL``). The validation tool does not validate ``CANCEL`` commands —
    they release reserved capital but do not propose new exposure, so the
    per-rule projection is a no-op and there is nothing to validate.
    """

    OPEN = "OPEN"
    ADD = "ADD"
    CLOSE = "CLOSE"
    ADJUST = "ADJUST"


class ValidationRequest(BaseModel):
    """A single proposal to validate.

    The minimal-by-design surface mirrors the spec's
    ``validate_guardrail(instrument, size, action)`` signature. Fields not in
    that triplet are tool-internal concerns:

    * ``sector`` is resolved by ``ValidationToolState.sector_resolver`` from the
      instrument's ticker.
    * ``existing_position_id`` is auto-resolved from the snapshot via the
      ticker/direction/asset-type lookup pattern (see
      ``_lookup_existing_position``).
    * ``daily_borrow_cost_usd`` is resolved from the state's
      ``borrow_cost_resolver`` for short equity OPEN/ADD; the request shape
      itself carries no borrow-cost field so the caller is not forced to know
      when the library's validators require one.
    * ``reserves_capital`` flags non-marketable limit OPENs that contribute to
      ``pending_order_capital_pct``; defaults to ``False`` so marketable orders
      need not opt out explicitly.
    """

    model_config = ConfigDict(frozen=True)

    instrument: ValidationInstrument
    size: ValidationSize
    action: ValidationAction
    reserves_capital: bool = False


class ValidationResult(BaseModel):
    """The tool's output, mirroring ``state-delivery.md`` § Output contract.

    ``implied_volatility`` is the IV the library consumed when computing
    ``greeks`` — the average across legs for OPTIONS / STRATEGY proposals,
    sourced from the IV surface (or realized-vol fallback) per
    ``iv_sourcing.py``. ``None`` for equity proposals (no IV needed) and for
    ADJUST/CANCEL/disabled-feature early-exits where greeks are zero/None.
    Plumbed through to :class:`alphamind.execution.oms.submit_envelope_mcp`
    ``Acknowledgment.validation_metadata.implied_volatility`` and on to
    persisted ``OptionGreeks.iv_used`` (ALP-399).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    overall: Literal["PASS", "FAIL"]
    per_rule: tuple[RuleProjection, ...]
    delta_adjusted_exposure: float
    greeks: Greeks | None
    implied_volatility: float | None
    cumulative_impact_note: str
    failure_guidance: str | None
    proposal_index_in_invocation: int


class ProjectedDelta(BaseModel):
    """A previously-validated proposal's projected impact, for cumulative tracking.

    Mirrors the ``ValidationRequest`` surface plus the cached projection result
    so subsequent calls can replay the proposal without re-running the
    library's projection math for the prior call's sector resolution. Borrow
    cost is re-resolved via ``ValidationToolState.borrow_cost_resolver``
    rather than cached here, mirroring the request-side flow.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    instrument: ValidationInstrument
    size: ValidationSize
    action: ValidationAction
    sector: str
    delta_adjusted_exposure: float
    greeks: Greeks | None
    proposal_index: int
    reserves_capital: bool = False
    existing_position_id: str | None = None


class ValidationToolError(Exception):
    """Raised when the validation tool cannot satisfy a structural precondition.

    The library validates its inputs and raises ``LibraryInputError``; this
    error class is the tool-layer twin for preconditions the tool itself
    enforces before calling the library — most notably that short equity
    proposals carry a ``borrow_cost_resolver`` on state.
    """


class ValidationToolState(BaseModel):
    """Per-invocation state passed across multiple ``validate_guardrail`` calls.

    The first three projection-shaped fields ARE the spec's renderer-shared
    inputs; callers can access them directly. They are also accessible via
    ``starting_snapshot.<derived attribute>`` once the snapshot exposes the
    full set — the named fields are caller ergonomics so the validation tool
    and the state-header renderers consume one canonical shape.

    The two ``library_*`` fields are library plumbing the agent runtime
    constructs once per invocation:
    ``library_config = from_resolved_config(resolved_config)`` and
    ``library_market = build_market_inputs(...)``. The tool composes
    ``evaluate_proposals`` against these without re-running adapter cost per
    ``validate_guardrail`` call.

    ``sector_resolver`` maps an instrument's ticker to its sector key — the
    tool resolves the proposal's sector internally so the public
    ``ValidationRequest`` shape can match the spec's
    ``validate_guardrail(instrument, size, action)`` signature.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    invocation_id: str
    # Spec-required (projection-shaped, shared with renderers):
    starting_snapshot: PortfolioStateSnapshot
    starting_risk_budget: RiskBudgetConsumption
    starting_active_risk_parameters: ActiveRiskParameterSet
    profile_feature_flags: FeatureFlagsView
    # Library plumbing (agent runtime constructs once via from_resolved_config
    # + market builder; cannot be derived from projection-shaped inputs alone
    # because ``LibraryConfig`` carries upstream-only fields like
    # escalation_zones and conservative_buffer_pct):
    library_config: LibraryConfig
    # ``MarketInputs`` carries an ``IvProvider`` Protocol that Pydantic cannot
    # generate validation for; ``SkipValidation`` prevents recursion into the
    # dataclass while ``arbitrary_types_allowed=True`` keeps the field assignable.
    library_market: SkipValidation[MarketInputs]
    # Tool helpers:
    sector_resolver: Callable[[str], str]
    # Borrow-cost resolver: required for short equity OPEN/ADD; ``None`` is
    # safe when the active profile disables short selling
    # (``profile_feature_flags.short_selling_enabled=False``) since the library
    # gate filters those proposals before borrow-cost lookup.
    #
    # Purity contract: must be a pure function — equal ``ticker`` inputs must
    # produce equal ``float`` outputs across all calls within a
    # ``validate_guardrail`` chain. The tool re-resolves borrow cost for every
    # accumulated short equity OPEN/ADD on each call (see
    # ``_projected_delta_to_library``); a closure over mutable state (live
    # ledger, network resolver without caching, mutating counter) violates this
    # contract and produces non-deterministic guidance across replays of the
    # same proposal sequence.
    borrow_cost_resolver: Callable[[str], float] | None = None
    accumulated_deltas: tuple[ProjectedDelta, ...] = ()

    @model_validator(mode="after")
    def _validate_feature_flags_agree(self) -> ValidationToolState:
        """Reject construction when ``profile_feature_flags`` and
        ``library_config.feature_flags`` diverge.

        The tool returns disabled-feature guidance against the former
        (``_disabled_feature_guidance``) and composes the library against the
        latter; silent divergence yields guidance/gate disagreement.
        """
        if self.profile_feature_flags != self.library_config.feature_flags:
            msg = (
                f"profile_feature_flags must equal library_config.feature_flags; "
                f"got profile={self.profile_feature_flags!r} vs "
                f"library={self.library_config.feature_flags!r}"
            )
            raise ValueError(msg)
        return self

    def with_accepted_proposal(self, delta: ProjectedDelta) -> ValidationToolState:
        """Return a new state with *delta* appended to ``accumulated_deltas``."""
        return self.model_copy(update={"accumulated_deltas": (*self.accumulated_deltas, delta)})


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------


_THIS_PROPOSAL_ID = "validation_request"


def validate_guardrail(
    *,
    request: ValidationRequest,
    state: ValidationToolState,
) -> ValidationResult:
    """Project *request* through the guardrails, accounting for prior proposals.

    Pure function — does not mutate *state*. The caller invokes
    ``state.with_accepted_proposal(...)`` after a PASS if it wants the proposal
    counted toward subsequent calls.
    """
    proposal_index = len(state.accumulated_deltas) + 1
    flags = state.profile_feature_flags
    is_options = request.instrument.asset_type in (
        InstrumentType.OPTIONS,
        InstrumentType.STRATEGY,
    )

    disabled_guidance = _disabled_feature_guidance(
        is_options=is_options,
        is_short=request.instrument.direction == Direction.SHORT,
        flags=flags,
    )
    if disabled_guidance is not None:
        return _disabled_result(
            guidance=disabled_guidance,
            cumulative_impact_note=_format_cumulative_impact_note(state),
            proposal_index=proposal_index,
        )

    proposals = (
        *(_projected_delta_to_library(d, state=state) for d in state.accumulated_deltas),
        _request_to_library_proposal(request, _THIS_PROPOSAL_ID, state=state),
    )

    output = evaluate_proposals(
        state=state.starting_snapshot,
        proposals=proposals,
        config=state.library_config,
        market=state.library_market,
    )

    this_dae = output.delta_adjusted[_THIS_PROPOSAL_ID]
    overall: Literal["PASS", "FAIL"] = (
        "FAIL" if any(p.status is Status.FAIL for p in output.per_rule) else "PASS"
    )

    failure_guidance: str | None = None
    if overall == "FAIL":
        failure_guidance = _format_failure_guidance(
            failed=tuple(p for p in output.per_rule if p.status is Status.FAIL),
            state=state,
            this_dae_usd=this_dae.signed_notional_usd,
        )

    return ValidationResult(
        overall=overall,
        per_rule=output.per_rule,
        delta_adjusted_exposure=this_dae.signed_notional_usd,
        greeks=this_dae.net_greeks,
        implied_volatility=this_dae.iv_used,
        cumulative_impact_note=_format_cumulative_impact_note(state),
        failure_guidance=failure_guidance,
        proposal_index_in_invocation=proposal_index,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disabled_feature_guidance(
    *,
    is_options: bool,
    is_short: bool,
    flags: FeatureFlagsView,
) -> str | None:
    """Return the agent-facing guidance for a disabled-feature early-exit, or None."""
    if is_options and not flags.options_enabled:
        return "Options trading is disabled for this portfolio profile."
    if is_short and not flags.short_selling_enabled:
        return "Short selling is disabled for this portfolio profile."
    return None


def _disabled_result(
    *,
    guidance: str,
    cumulative_impact_note: str,
    proposal_index: int,
) -> ValidationResult:
    """Build the early-exit ``ValidationResult`` for a disabled-feature rejection."""
    return ValidationResult(
        overall="FAIL",
        per_rule=(),
        delta_adjusted_exposure=0.0,
        greeks=None,
        implied_volatility=None,
        cumulative_impact_note=cumulative_impact_note,
        failure_guidance=guidance,
        proposal_index_in_invocation=proposal_index,
    )


_INSTRUMENT_TO_ASSET_TYPE: dict[InstrumentType, LibraryAssetType] = {
    InstrumentType.EQUITY: LibraryAssetType.EQUITY,
    InstrumentType.OPTIONS: LibraryAssetType.OPTION,
    InstrumentType.STRATEGY: LibraryAssetType.STRATEGY,
}

_DIRECTION_TO_LIBRARY: dict[Direction, LibraryDirection] = {
    Direction.LONG: LibraryDirection.LONG,
    Direction.SHORT: LibraryDirection.SHORT,
}

_ACTION_TO_LIBRARY: dict[ValidationAction, LibraryAction] = {
    ValidationAction.OPEN: LibraryAction.OPEN,
    ValidationAction.ADD: LibraryAction.ADD,
    ValidationAction.CLOSE: LibraryAction.CLOSE,
    ValidationAction.ADJUST: LibraryAction.ADJUST,
}

_CONTRACT_TYPE_TO_LIBRARY: dict[str, LibraryContractType] = {
    "call": LibraryContractType.CALL,
    "put": LibraryContractType.PUT,
}


def _format_cumulative_impact_note(state: ValidationToolState) -> str:
    prior = len(state.accumulated_deltas)
    this_index = prior + 1
    if prior == 0:
        return (
            f"This is proposal #{this_index} in this invocation. "
            f"No prior proposals affect headroom calculations."
        )
    return (
        f"This is proposal #{this_index} in this invocation. "
        f"Cumulative impact of proposals #1-{prior} is included in headroom calculations."
    )


def _request_to_library_proposal(
    request: ValidationRequest,
    proposal_id: str,
    *,
    state: ValidationToolState,
) -> ProposedDelta:
    """Convert a ``ValidationRequest`` to the library's ``ProposedDelta`` shape.

    Resolves the proposal's sector via ``state.sector_resolver`` and looks up
    any matching existing position from ``state.starting_snapshot`` based on
    ticker / direction / asset-type. The library validates that ADD/CLOSE/
    ADJUST actions carry a position id; the lookup returns ``None`` for
    OPEN actions.

    For short equity OPEN/ADD, derives ``daily_borrow_cost_usd`` via
    ``state.borrow_cost_resolver``. For options, the proposal's
    ``notional_usd`` carries the premium-at-risk (``size.premium_at_risk_usd``
    when set, else ``size.dollar_value``) — the library treats option
    ``notional_usd`` as the cash impact / premium-at-risk for capital
    accounting per ``rules/capital.py``.
    """
    instrument = request.instrument
    return ProposedDelta(
        id=proposal_id,
        underlying=instrument.ticker,
        sector=state.sector_resolver(instrument.ticker),
        direction=_DIRECTION_TO_LIBRARY[instrument.direction],
        asset_type=_INSTRUMENT_TO_ASSET_TYPE[instrument.asset_type],
        notional_usd=_library_notional_usd(instrument, request.size),
        quantity=float(request.size.quantity),
        option_legs=_build_option_legs(instrument, request.size.quantity),
        action=_ACTION_TO_LIBRARY[request.action],
        existing_position_id=_lookup_existing_position(request, snapshot=state.starting_snapshot),
        daily_borrow_cost_usd=_resolve_borrow_cost(
            instrument=instrument, action=request.action, state=state
        ),
        reserves_capital=request.reserves_capital,
    )


def _library_notional_usd(instrument: ValidationInstrument, size: ValidationSize) -> float:
    """Compute the library ``notional_usd`` for a proposal.

    Equity: ``size.dollar_value``. Options/strategy: ``size.premium_at_risk_usd``
    when set, else ``size.dollar_value`` as a fallback. The library's
    ``rules/capital.py`` consumes options ``notional_usd`` as premium-at-risk
    for cash accounting; piping the spec's ``premium_at_risk_usd`` keeps the
    semantics aligned across the layers.
    """
    is_option_like = instrument.asset_type in (InstrumentType.OPTIONS, InstrumentType.STRATEGY)
    if is_option_like and size.premium_at_risk_usd is not None:
        return size.premium_at_risk_usd
    return size.dollar_value


def _resolve_borrow_cost(
    *,
    instrument: ValidationInstrument,
    action: ValidationAction,
    state: ValidationToolState,
) -> float | None:
    """Return the borrow cost for short equity OPEN/ADD; ``None`` otherwise.

    Raises ``ValidationToolError`` when the resolver is missing on a request
    that requires it. Other actions (LONG, options, CLOSE/ADJUST) return
    ``None``; the library's borrow-cost rule reads the proposal field only
    where applicable.
    """
    needs_borrow_cost = (
        instrument.direction == Direction.SHORT
        and instrument.asset_type == InstrumentType.EQUITY
        and action in (ValidationAction.OPEN, ValidationAction.ADD)
    )
    if not needs_borrow_cost:
        return None
    resolver = state.borrow_cost_resolver
    if resolver is None:
        msg = (
            f"borrow_cost_resolver is required on ValidationToolState for "
            f"short equity {action.value} (ticker={instrument.ticker!r})"
        )
        raise ValidationToolError(msg)
    return resolver(instrument.ticker)


_POSITION_LOOKUP_ACTIONS = frozenset(
    {ValidationAction.ADD, ValidationAction.CLOSE, ValidationAction.ADJUST}
)


def _lookup_existing_position(
    request: ValidationRequest, *, snapshot: PortfolioStateSnapshot
) -> str | None:
    """Resolve an existing position id from the snapshot for ADD/CLOSE/ADJUST.

    OPEN actions never reference an existing position (the library validates
    this). For other actions, scans ``snapshot.existing_positions`` for a
    single position matching the proposal's ticker / direction / asset-type;
    returns its id if exactly one matches, else ``None``. The library raises
    a structured input error when ``None`` is returned for an action that
    requires it, surfacing the caller-side ambiguity at the right layer.
    """
    if request.action not in _POSITION_LOOKUP_ACTIONS:
        return None
    target_underlying = request.instrument.ticker
    target_direction = _DIRECTION_TO_LIBRARY[request.instrument.direction]
    target_asset_type = _INSTRUMENT_TO_ASSET_TYPE[request.instrument.asset_type]
    matches = [
        pos
        for pos in snapshot.existing_positions.values()
        if pos.underlying == target_underlying
        and pos.direction == target_direction
        and pos.asset_type == target_asset_type
    ]
    if len(matches) == 1:
        return matches[0].position_id
    return None


def _build_option_legs(
    instrument: ValidationInstrument, quantity: int
) -> tuple[OptionLeg, ...] | None:
    """Build the library's ``OptionLeg`` tuple from an instrument shape.

    ``ValidationInstrument`` validators guarantee the per-asset-type fields
    populated, so each branch's None-narrowing reads are sound.
    """
    if instrument.asset_type == InstrumentType.EQUITY:
        return None
    if instrument.asset_type == InstrumentType.OPTIONS:
        return (
            _make_option_leg(
                direction=instrument.direction,
                strike=instrument.strike,
                expiration=instrument.expiration,
                contract_type=instrument.contract_type,
                quantity=quantity,
            ),
        )
    # STRATEGY: validator guarantees legs is non-None and non-empty.
    legs = instrument.legs or ()
    return tuple(
        _make_option_leg(
            direction=leg.direction,
            strike=leg.strike,
            expiration=leg.expiration,
            contract_type=leg.contract_type,
            quantity=leg.quantity,
        )
        for leg in legs
    )


def _make_option_leg(
    *,
    direction: Direction,
    strike: float | None,
    expiration: datetime | None,
    contract_type: Literal["call", "put"] | None,
    quantity: int,
) -> OptionLeg:
    """Build one library ``OptionLeg`` from validated instrument fields."""
    if strike is None or expiration is None or contract_type is None:
        msg = "options leg requires strike, expiration, and contract_type"
        raise ValueError(msg)
    leg_sign = 1 if direction == Direction.LONG else -1
    return OptionLeg(
        contract_type=_CONTRACT_TYPE_TO_LIBRARY[contract_type],
        strike=strike,
        expiration=expiration.date(),
        quantity=leg_sign * quantity,
    )


def _projected_delta_to_library(
    delta: ProjectedDelta, *, state: ValidationToolState
) -> ProposedDelta:
    """Convert a previously-accepted proposal back to the library shape.

    Replays the cached sector / position-id values; re-resolves borrow cost
    via ``state.borrow_cost_resolver`` so prior short equity OPEN/ADD
    proposals reproduce identical library inputs across calls. The resolver
    is total within an invocation (the active state object holds the same
    resolver across all calls), so repeated lookups are deterministic.
    """
    instrument = delta.instrument
    return ProposedDelta(
        id=f"prior_{delta.proposal_index}",
        underlying=instrument.ticker,
        sector=delta.sector,
        direction=_DIRECTION_TO_LIBRARY[instrument.direction],
        asset_type=_INSTRUMENT_TO_ASSET_TYPE[instrument.asset_type],
        notional_usd=_library_notional_usd(instrument, delta.size),
        quantity=float(delta.size.quantity),
        option_legs=_build_option_legs(instrument, delta.size.quantity),
        action=_ACTION_TO_LIBRARY[delta.action],
        existing_position_id=delta.existing_position_id,
        daily_borrow_cost_usd=_resolve_borrow_cost(
            instrument=instrument, action=delta.action, state=state
        ),
        reserves_capital=delta.reserves_capital,
    )


# ---------------------------------------------------------------------------
# Failure-guidance templates
# ---------------------------------------------------------------------------


_DIRECTIONAL_RULES = frozenset({"net_long_pct", "net_short_pct", "gross_exposure_pct"})
_CAPITAL_RULES = frozenset({"min_cash_reserve_pct", "pending_order_capital_pct"})


def _breach_magnitude(projection: RuleProjection) -> float:
    """Absolute distance from the limit, positive for any failed projection.

    Standard rules breach above the cap (``projected_after > limit``); inverse
    rules breach below the floor (``projected_after < limit``). Both directions
    map to a positive magnitude so multi-rule primary selection compares like
    quantities.
    """
    return abs(projection.projected_after - projection.limit)


def _reduction_pct(projection: RuleProjection) -> int:
    """Percent reduction needed to bring the projection back inside the limit.

    For standard rules: reduction relative to ``projected_after`` (how much of
    the projected exposure must be shed). For inverse rules: relative to the
    floor (how much extra cash, as a fraction of the floor, must be preserved).
    Both branches return a non-negative integer.
    """
    magnitude = _breach_magnitude(projection)
    denominator = projection.limit if projection.inverse else projection.projected_after
    if denominator == 0:
        return 0
    return round(magnitude / abs(denominator) * 100)


def _format_failure_guidance(
    *,
    failed: tuple[RuleProjection, ...],
    state: ValidationToolState,
    this_dae_usd: float,
) -> str:
    """Map failed per-rule projections to an agent-friendly action recommendation."""
    if len(failed) == 1:
        return _single_rule_guidance(failed[0], state=state, this_dae_usd=this_dae_usd)
    return _multi_rule_guidance(failed=failed)


def _single_rule_guidance(
    projection: RuleProjection,
    *,
    state: ValidationToolState,
    this_dae_usd: float,
) -> str:
    rule = projection.rule
    if rule in _CAPITAL_RULES:
        return _capital_rule_guidance(projection, state=state, this_dae_usd=this_dae_usd)
    pct = _reduction_pct(projection)
    if rule in _DIRECTIONAL_RULES:
        return f"Reduce size by ~{pct}% to pass {rule}, or substitute a lower-delta instrument"
    return f"Reduce size by ~{pct}% to pass {rule}"


def _capital_rule_guidance(
    projection: RuleProjection,
    *,
    state: ValidationToolState,
    this_dae_usd: float,
) -> str:
    """Format the capital-rule failure message.

    Inverse rules (``min_cash_reserve_pct``): ``limit`` is a floor on cash %.
    Available capital above the floor is ``(current - limit) * portfolio /
    100``; the proposal pushed cash ``(limit - projected_after) * portfolio /
    100`` USD below the floor.

    Standard rules (``pending_order_capital_pct``): ``limit`` is a cap on
    reserved %. Available capital below the cap is ``(limit - current) *
    portfolio / 100``; the proposal exceeded the cap by ``(projected_after -
    limit) * portfolio / 100`` USD.

    In both cases the suggested smaller dollar value is the proposal's
    absolute notional minus the breach magnitude in USD.
    """
    portfolio = state.starting_snapshot.portfolio_value_usd
    if projection.inverse:
        available_usd = max(0.0, (projection.current - projection.limit)) * portfolio / 100.0
        shortfall_usd = (projection.limit - projection.projected_after) * portfolio / 100.0
    else:
        available_usd = max(0.0, (projection.limit - projection.current)) * portfolio / 100.0
        shortfall_usd = (projection.projected_after - projection.limit) * portfolio / 100.0
    suggested_usd = max(0.0, abs(this_dae_usd) - shortfall_usd)
    return (
        f"Insufficient deployable capital (${available_usd:,.0f}); "
        f"reduce dollar value to ~${suggested_usd:,.0f} or fewer"
    )


def _multi_rule_guidance(*, failed: tuple[RuleProjection, ...]) -> str:
    primary = max(failed, key=_breach_magnitude)
    pct = _reduction_pct(primary)
    rules = ", ".join(p.rule for p in failed)
    return (
        f"Multiple rules would breach: {rules}. "
        f"Reducing size by ~{pct}% addresses {primary.rule}; "
        f"revise instrument or skip to address others."
    )
