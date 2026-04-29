"""Secondary-breach checking for proposed protective closes (story 05b).

Pure function ``check_secondary_breach`` that, before a protective CLOSE is
finalized, projects the close through guardrail-evaluation's
``evaluate_proposals`` and identifies any *newly-introduced* FAIL'd rules — i.e.,
rules whose status was PASS or WARNING pre-close but FAIL post-close — excluding
the rule the close is intended to cure.

The function consumes the guardrail-evaluation primitive via the structural
Protocols below so this story stays decoupled from the upstream package's
exact module path; the Protocols match the shipped library shapes verbatim.

See ``docs/implementation/06-risk-guardrails/breach-behavior/05b-secondary-breach-check.md``
for the authoritative contract and worked examples.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from alphamind.risk_guardrails.breach_behavior.config import BreachBehaviorConfig
from alphamind.risk_guardrails.breach_behavior.hard_rejection import (
    LibraryOutputProtocol,
    RuleProjectionProtocol,
)
from alphamind.risk_guardrails.breach_behavior.types import (
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
)

_FAIL_STATUS = "FAIL"


# ---------------------------------------------------------------------------
# Structural Protocols — match the guardrail-evaluation library verbatim
# ---------------------------------------------------------------------------


class PortfolioStateSnapshotProtocol(Protocol):
    """Structural shape of the portfolio snapshot the library projects against.

    Threaded through opaquely; the primitive itself reads no fields, but the
    library callable does. Production callers pass
    ``alphamind.risk_guardrails.guardrail_evaluation.PortfolioStateSnapshot``.
    """


class LibraryConfigProtocol(Protocol):
    """Structural shape of the library config carved from ``ResolvedConfig``.

    The primitive reads ``effective_limits`` to validate that
    ``primary_breach_rule_id`` belongs to the active rule set.
    """

    @property
    def effective_limits(self) -> Mapping[str, float]: ...


class MarketInputsProtocol(Protocol):
    """Structural shape of the library's market-data inputs. Opaque to the primitive."""


class ProposedDeltaProtocol(Protocol):
    """Structural shape the library callable accepts.

    Production callers pass real
    ``alphamind.risk_guardrails.guardrail_evaluation.ProposedDelta`` instances;
    the primitive constructs a CLOSE-action delta-shaped object via
    ``_build_close_delta``.
    """


class EvaluateProposalsCallable(Protocol):
    """Structural shape of guardrail-evaluation's ``evaluate_proposals``.

    Matches the production signature in
    ``alphamind.risk_guardrails.guardrail_evaluation.evaluate_proposals``.
    """

    def __call__(
        self,
        *,
        state: PortfolioStateSnapshotProtocol,
        proposals: Sequence[ProposedDeltaProtocol],
        config: LibraryConfigProtocol,
        market: MarketInputsProtocol,
    ) -> LibraryOutputProtocol: ...


# ---------------------------------------------------------------------------
# ProposedClose value object
# ---------------------------------------------------------------------------


class ProposedClose(BaseModel):
    """A protective close awaiting secondary-breach validation.

    Translates from a ``PositionSelectionResult`` into a library-compatible
    delta description. ``close_size_*`` fields equal ``pre_close_size_*`` for
    a FULL_CLOSE and are smaller for a PARTIAL_TRIM.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    ticker: str
    asset_type: Literal["equity", "option", "strategy"]
    direction: Literal["long", "short"]
    pre_close_size_pct_of_portfolio: float
    close_size_pct_of_portfolio: float
    pre_close_size_usd: float
    close_size_usd: float

    @model_validator(mode="after")
    def _validate_close_within_position(self) -> ProposedClose:
        if self.close_size_pct_of_portfolio > self.pre_close_size_pct_of_portfolio + 1e-9:
            msg = (
                f"close_size_pct_of_portfolio ({self.close_size_pct_of_portfolio}) "
                f"exceeds pre_close_size_pct_of_portfolio "
                f"({self.pre_close_size_pct_of_portfolio})"
            )
            raise ValueError(msg)
        if self.close_size_usd > self.pre_close_size_usd + 1e-3:
            msg = (
                f"close_size_usd ({self.close_size_usd}) exceeds "
                f"pre_close_size_usd ({self.pre_close_size_usd})"
            )
            raise ValueError(msg)
        if self.close_size_pct_of_portfolio <= 0:
            msg = f"close_size_pct_of_portfolio must be > 0; got {self.close_size_pct_of_portfolio}"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Secondary-breach check
# ---------------------------------------------------------------------------


def check_secondary_breach(
    *,
    proposed_close: ProposedClose,
    current_state: PortfolioStateSnapshotProtocol,
    library_config: LibraryConfigProtocol,
    market_inputs: MarketInputsProtocol,
    primary_breach_rule_id: str,
    config: BreachBehaviorConfig,
    evaluate_proposals: EvaluateProposalsCallable,
) -> SecondaryBreachCheckResult:
    """Project a proposed protective close through the guardrail-evaluation library.

    Composes ``evaluate_proposals`` twice — once on baseline (no proposals) and
    once with the close as a CLOSE-action ``ProposedDelta`` — diffs the per-rule
    statuses, and classifies the outcome.

    Args:
        proposed_close: The close being evaluated.
        current_state: Portfolio state snapshot the close evaluates against.
        library_config: Guardrail-evaluation library config for the active
            profile/regime.
        market_inputs: Market data for the library.
        primary_breach_rule_id: The rule the protective close is curing;
            excluded from secondary-breach detection.
        config: Breach-behavior config. ``delta_buffer_secondary_check_buffer_factor``
            scales the library's delta buffer for this re-evaluation; default
            ``1.0`` reuses the standard buffer.
        evaluate_proposals: The guardrail-evaluation entry point. Injected so
            tests can stub the library; production callers pass
            ``alphamind.risk_guardrails.guardrail_evaluation.evaluate_proposals``.

    Returns:
        A frozen ``SecondaryBreachCheckResult`` with ``result`` set to one of
        ``NO_SECONDARY_BREACH`` or ``DEFERRED_TO_PM`` (this primitive never
        emits ``SECONDARY_BREACH_AVOIDED``; that's story 07's responsibility).
    """
    # TODO: thread ``config.delta_buffer_secondary_check_buffer_factor`` through
    # to ``evaluate_proposals`` once the library accepts a runtime delta-buffer
    # override. Default 1.0 reproduces the library's standard buffer until then.
    _ = config.delta_buffer_secondary_check_buffer_factor
    if primary_breach_rule_id not in library_config.effective_limits:
        msg = (
            f"primary_breach_rule_id {primary_breach_rule_id!r} is not in the "
            f"library's active effective_limits"
        )
        raise ValueError(msg)
    baseline = _invoke_library(
        evaluate_proposals,
        state=current_state,
        proposals=(),
        config=library_config,
        market=market_inputs,
        phase="baseline",
    )
    close_delta = _build_close_delta(proposed_close)
    post_close = _invoke_library(
        evaluate_proposals,
        state=current_state,
        proposals=(close_delta,),
        config=library_config,
        market=market_inputs,
        phase="post-close",
    )
    introduced = _newly_failed_rules(
        baseline=baseline.per_rule,
        post_close=post_close.per_rule,
        primary_breach_rule_id=primary_breach_rule_id,
    )
    if introduced:
        return SecondaryBreachCheckResult(
            result=SecondaryBreachOutcome.DEFERRED_TO_PM,
            notes=f"secondary breach introduced on: {', '.join(introduced)}",
        )
    return SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.NO_SECONDARY_BREACH,
        notes="post-close projection introduced no new FAIL'd rules",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _invoke_library(
    evaluate_proposals: EvaluateProposalsCallable,
    *,
    state: PortfolioStateSnapshotProtocol,
    proposals: Sequence[ProposedDeltaProtocol],
    config: LibraryConfigProtocol,
    market: MarketInputsProtocol,
    phase: str,
) -> LibraryOutputProtocol:
    """Call the guardrail-evaluation library; wrap any exception with secondary-check context."""
    try:
        return evaluate_proposals(state=state, proposals=proposals, config=config, market=market)
    except Exception as exc:
        msg = f"secondary-breach check failed during {phase} evaluation: {exc}"
        raise ValueError(msg) from exc


def _build_close_delta(proposed_close: ProposedClose) -> ProposedDeltaProtocol:
    """Translate a ``ProposedClose`` into a ``ProposedDelta``-shaped object.

    The library's ``ProposedDelta`` dataclass is frozen with ``slots=True``, so
    the return is a structurally-compatible plain object rather than a direct
    construction (which would couple this primitive to the upstream module
    path). Production callers using the real library will type-check via
    structural compatibility with ``ProposedDeltaProtocol``.
    """
    return _CloseDelta(
        id=f"secondary_check.{proposed_close.position_id}",
        existing_position_id=proposed_close.position_id,
        underlying=proposed_close.ticker,
        notional_usd=proposed_close.close_size_usd,
        quantity=proposed_close.close_size_usd,
        action="CLOSE",
        direction=proposed_close.direction.upper(),
        asset_type=proposed_close.asset_type.upper(),
    )


@dataclass(frozen=True, slots=True)
class _CloseDelta:
    """Internal duck-typed proposed-delta record for the secondary check."""

    id: str
    existing_position_id: str
    underlying: str
    notional_usd: float
    quantity: float
    action: str
    direction: str
    asset_type: str


def _newly_failed_rules(
    *,
    baseline: tuple[RuleProjectionProtocol, ...],
    post_close: tuple[RuleProjectionProtocol, ...],
    primary_breach_rule_id: str,
) -> tuple[str, ...]:
    """Return rule IDs that flipped to FAIL post-close (excluding the primary rule).

    A rule is *newly failed* when its baseline status is non-FAIL and its
    post-close status is FAIL. Pre-existing FAIL'd rules and the
    ``primary_breach_rule_id`` are excluded.
    """
    baseline_status = {r.rule: r.status for r in baseline}
    introduced: list[str] = []
    for rule in post_close:
        if rule.rule == primary_breach_rule_id:
            continue
        if rule.status != _FAIL_STATUS:
            continue
        if baseline_status.get(rule.rule) == _FAIL_STATUS:
            continue
        introduced.append(rule.rule)
    return tuple(introduced)


__all__ = [
    "EvaluateProposalsCallable",
    "LibraryConfigProtocol",
    "LibraryOutputProtocol",
    "MarketInputsProtocol",
    "PortfolioStateSnapshotProtocol",
    "ProposedClose",
    "ProposedDeltaProtocol",
    "RuleProjectionProtocol",
    "check_secondary_breach",
]
