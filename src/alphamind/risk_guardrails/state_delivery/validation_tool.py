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

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, SkipValidation, model_validator

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
    """A single proposal to validate."""

    model_config = ConfigDict(frozen=True)

    instrument: ValidationInstrument
    size: ValidationSize
    action: ValidationAction
    sector: str
    existing_position_id: str | None = None
    daily_borrow_cost_usd: float | None = None


class ValidationResult(BaseModel):
    """The tool's output, mirroring ``state-delivery.md`` § Output contract."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    overall: Literal["PASS", "FAIL"]
    per_rule: tuple[RuleProjection, ...]
    delta_adjusted_exposure: float
    greeks: Greeks | None
    cumulative_impact_note: str
    failure_guidance: str | None
    proposal_index_in_invocation: int


class ProjectedDelta(BaseModel):
    """A previously-validated proposal's projected impact, for cumulative tracking."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    instrument: ValidationInstrument
    size: ValidationSize
    action: ValidationAction
    sector: str
    delta_adjusted_exposure: float
    greeks: Greeks | None
    proposal_index: int


class ValidationToolState(BaseModel):
    """Per-invocation state passed across multiple ``validate_guardrail`` calls."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    invocation_id: str
    starting_snapshot: SkipValidation[PortfolioStateSnapshot]
    starting_config: SkipValidation[LibraryConfig]
    starting_market: SkipValidation[MarketInputs]
    accumulated_deltas: tuple[ProjectedDelta, ...] = ()

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
    flags = state.starting_config.feature_flags
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
        *(_projected_delta_to_library(d) for d in state.accumulated_deltas),
        _request_to_library_proposal(request, _THIS_PROPOSAL_ID),
    )

    output = evaluate_proposals(
        state=state.starting_snapshot,
        proposals=proposals,
        config=state.starting_config,
        market=state.starting_market,
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


def _request_to_library_proposal(request: ValidationRequest, proposal_id: str) -> ProposedDelta:
    """Convert a ``ValidationRequest`` to the library's ``ProposedDelta`` shape."""
    return ProposedDelta(
        id=proposal_id,
        underlying=request.instrument.ticker,
        sector=request.sector,
        direction=_DIRECTION_TO_LIBRARY[request.instrument.direction],
        asset_type=_INSTRUMENT_TO_ASSET_TYPE[request.instrument.asset_type],
        notional_usd=request.size.dollar_value,
        quantity=float(request.size.quantity),
        option_legs=_build_option_legs(request),
        action=_ACTION_TO_LIBRARY[request.action],
        existing_position_id=request.existing_position_id,
        daily_borrow_cost_usd=request.daily_borrow_cost_usd,
    )


def _build_option_legs(request: ValidationRequest) -> tuple[OptionLeg, ...] | None:
    """Build the library's ``OptionLeg`` tuple from the request's instrument shape.

    ``ValidationInstrument`` validators guarantee the per-asset-type fields
    populated, so each branch's None-narrowing reads are sound.
    """
    instrument = request.instrument
    if instrument.asset_type == InstrumentType.EQUITY:
        return None
    if instrument.asset_type == InstrumentType.OPTIONS:
        return (
            _make_option_leg(
                direction=instrument.direction,
                strike=instrument.strike,
                expiration=instrument.expiration,
                contract_type=instrument.contract_type,
                quantity=request.size.quantity,
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


def _projected_delta_to_library(delta: ProjectedDelta) -> ProposedDelta:
    """Convert a previously-accepted proposal back to the library shape."""
    request = ValidationRequest(
        instrument=delta.instrument,
        size=delta.size,
        action=delta.action,
        sector=delta.sector,
    )
    return _request_to_library_proposal(request, f"prior_{delta.proposal_index}")


# ---------------------------------------------------------------------------
# Failure-guidance templates
# ---------------------------------------------------------------------------


_DIRECTIONAL_RULES = frozenset({"net_long_pct", "net_short_pct", "gross_exposure_pct"})
_CAPITAL_RULES = frozenset({"min_cash_reserve_pct", "pending_order_capital_pct"})


def _reduction_pct(projection: RuleProjection) -> int:
    """Compute the percent reduction needed to bring projected_after below limit."""
    overage = projection.projected_after - projection.limit
    if projection.projected_after == 0:
        return 0
    return round(overage / projection.projected_after * 100)


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
        portfolio = state.starting_snapshot.portfolio_value_usd
        available_usd = projection.limit * portfolio / 100.0
        shortfall_usd = (projection.limit - projection.projected_after) * portfolio / 100.0
        suggested_usd = max(0.0, abs(this_dae_usd) - shortfall_usd)
        return (
            f"Insufficient deployable capital (${available_usd:,.0f}); "
            f"reduce dollar value to ~${suggested_usd:,.0f} or fewer"
        )
    pct = _reduction_pct(projection)
    if rule in _DIRECTIONAL_RULES:
        return f"Reduce size by ~{pct}% to pass {rule}, or substitute a lower-delta instrument"
    return f"Reduce size by ~{pct}% to pass {rule}"


def _multi_rule_guidance(*, failed: tuple[RuleProjection, ...]) -> str:
    primary = max(failed, key=lambda p: p.projected_after - p.limit)
    pct = _reduction_pct(primary)
    rules = ", ".join(p.rule for p in failed)
    return (
        f"Multiple rules would breach: {rules}. "
        f"Reducing size by ~{pct}% addresses {primary.rule}; "
        f"revise instrument or skip to address others."
    )
