"""Wire-format submission-result shapes — engine ⇄ LLM contract types.

Per ``docs/design/04-decision-layer/submit-envelope-tool-schema.md``, every
``submit_envelope`` call produces one :class:`SubmissionResult` per embedded
OMS command — either accepted with an :class:`Acknowledgment` or rejected
with a :class:`RejectionPayload`. The shapes are identical for the engine-
stub and the future real-engine path so the PM's feedback-handling code
treats stub and real-engine results uniformly.

The types are wire-format-only and carry no first-party dependencies
outside the kernel — they sit in :mod:`alphamind.commands` so both
decision-side producers (the engine-stub MCP wrapper at
:mod:`alphamind.decision.portfolio_manager.submit_envelope`) and
execution-side consumers (:mod:`alphamind.execution.state_persistence
.write_paths.phase2`, :mod:`alphamind.execution.oms.submit_engine_envelope`)
can import them without re-introducing the decision↔execution cycle that
ALP-458 eliminated.

The ``greeks`` field on :class:`_ValidationMetadata` and
:class:`RejectionPayload` is typed structurally via :class:`GreeksLike` —
the concrete dataclass lives in
:mod:`alphamind.risk_guardrails.guardrail_evaluation` and duck-types this
Protocol. Kernel modules cannot import risk_guardrails directly without
re-opening the cycle.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from alphamind._kernel.ids import OrderId, PositionId

__all__ = [
    "Acknowledgment",
    "GreeksLike",
    "RejectionPayload",
    "SubmissionResult",
    "_BreachedRule",
    "_PerRuleHeadroomEntry",
    "_ValidationMetadata",
]


# ---------------------------------------------------------------------------
# Structural Greeks shape — kernel modules cannot import risk_guardrails;
# the real :class:`alphamind.risk_guardrails.guardrail_evaluation.Greeks`
# dataclass satisfies this Protocol by field shape.
# ---------------------------------------------------------------------------


@runtime_checkable
class GreeksLike(Protocol):
    """Structural shape of a Greeks bundle.

    The concrete :class:`alphamind.risk_guardrails.guardrail_evaluation.Greeks`
    frozen dataclass duck-types this Protocol — kernel-level submission-result
    shapes can carry the values without importing risk_guardrails.
    """

    delta: float
    gamma: float
    theta: float
    vega: float


# ---------------------------------------------------------------------------
# Per-command result shapes — minimal Pydantic models mirroring the design doc
# ---------------------------------------------------------------------------


class _PerRuleHeadroomEntry(BaseModel):
    """One projected per-rule headroom entry; appears on Acknowledgment and
    RejectionPayload."""

    model_config = ConfigDict(frozen=True)

    rule: str
    headroom_remaining: float
    unit: str


class _ValidationMetadata(BaseModel):
    """Validation-time computation results attached to OPEN/ADD acknowledgments.

    The ``greeks`` field carries a :class:`GreeksLike`-shaped object —
    concrete type is :class:`alphamind.risk_guardrails.guardrail_evaluation
    .Greeks`. Typed as :class:`Any` here because Pydantic v2 model validation
    of the Protocol shape requires ``arbitrary_types_allowed`` plumbing that
    would force every nested model to opt in; the actual values are passed
    through unmodified.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    greeks: Any | None = None
    implied_volatility: float | None = None
    delta_adjusted_exposure: float
    per_rule_headroom: tuple[_PerRuleHeadroomEntry, ...]


class Acknowledgment(BaseModel):
    """Engine confirmation for an accepted command.

    Per ``submit-envelope-tool-schema.md`` § acknowledgment, the populated
    fields vary by command type — OPEN / ADD carry ``validation_metadata``;
    CANCEL carries ``released_capital_usd``. Rather than encode a discriminated
    union for the engine-stub, we model the union flat and leave inapplicable
    fields ``None``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    position_id: PositionId | None = None
    order_id: OrderId | None = None
    validation_metadata: _ValidationMetadata | None = None
    released_capital_usd: float | None = None


class _BreachedRule(BaseModel):
    """One breached-rule entry on a RejectionPayload."""

    model_config = ConfigDict(frozen=True)

    rule: str
    current: float
    limit: float
    overage: float
    unit: str


class RejectionPayload(BaseModel):
    """Synchronous rejection record per breach-behavior.md § Hard rejection
    semantics. Identical shape to the engine-side rejection so the PM's
    feedback handling treats stub and real-engine rejections uniformly.

    ``gateway_reason`` carries the broker's
    :class:`alphamind.execution.broker_adapter.PermanentRejection` code
    (e.g. ``"insufficient_buying_power"``) when the rejection originated at
    the broker after Layer-1/2/3 validation accepted the command — story 03e
    (ALP-390) coordinated swap. ``None`` for guardrail-side rejections.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    rules_breached: tuple[_BreachedRule, ...]
    suggested_modification: str
    headroom_after_suggestion: tuple[_PerRuleHeadroomEntry, ...] = ()
    greeks: Any | None = None
    delta_adjusted_exposure: float | None = None
    feature_disabled: Literal["options", "short_selling", "sector"] | None = None
    gateway_reason: str | None = None


class SubmissionResult(BaseModel):
    """One per-command result mirroring submit-envelope-tool-schema.md's
    submission_result $def."""

    model_config = ConfigDict(frozen=True)

    command_ordinal: int
    status: Literal["accepted", "rejected"]
    command_id: str
    acknowledgment: Acknowledgment | None = None
    rejection_payload: RejectionPayload | None = None
