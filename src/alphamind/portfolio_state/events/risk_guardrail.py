"""Risk and guardrail event details — rejections, limit approaches, halts, emergencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from alphamind.portfolio_state.events.types import EventGroup, EventType

_HALT_TYPE_LITERAL = Literal["daily_drawdown", "cumulative_drawdown_tier3"]


@dataclass(frozen=True, slots=True)
class GuardrailRejectionDetail:
    """Detail payload for GUARDRAIL_REJECTION events."""

    command_summary: str
    blocking_rule_ids: tuple[str, ...]
    current_limit_values_json: dict[str, float]
    headroom_json: dict[str, float]
    suggested_modification: str | None


@dataclass(frozen=True, slots=True)
class RiskLimitApproachedDetail:
    """Detail payload for RISK_LIMIT_APPROACHED events."""

    metric_id: str
    current_value: float
    threshold_value: float
    limit_value: float


@dataclass(frozen=True, slots=True)
class RiskParameterChangedDetail:
    """Detail payload for RISK_PARAMETER_CHANGED events."""

    old_parameter_set_json: dict[str, Any]
    new_parameter_set_json: dict[str, Any]
    regime_label: str


@dataclass(frozen=True, slots=True)
class HaltActivatedDetail:
    """Detail payload for ``HALT_ACTIVATED`` events.

    Emitted by the continuous monitor's breach-evaluation loop (story 03b /
    ALP-437) on the inactive→active halt transition. ``halt_type`` discriminates
    daily-drawdown halts (drawdown reaches 100% of daily limit) from cumulative
    tier-3 halts (cumulative drawdown reaches the ``FULL_HALT`` tier threshold).
    ``current_drawdown_pct`` is the observed drawdown at the transition;
    ``limit_pct`` is the limit value the drawdown crossed. ``detected_at`` is
    tz-aware UTC — the tick timestamp the loop used for evaluation.
    """

    halt_type: _HALT_TYPE_LITERAL
    current_drawdown_pct: float
    limit_pct: float
    detected_at: datetime

    def __post_init__(self) -> None:
        if self.detected_at.tzinfo is None:
            msg = "detected_at must be tz-aware UTC"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class HaltLiftedDetail:
    """Detail payload for ``HALT_LIFTED`` events.

    Symmetric with :class:`HaltActivatedDetail` — emitted on the active→inactive
    halt transition. ``current_drawdown_pct`` is the drawdown reading at the
    lift; ``lifted_at`` is the tick timestamp.
    """

    halt_type: _HALT_TYPE_LITERAL
    current_drawdown_pct: float
    lifted_at: datetime

    def __post_init__(self) -> None:
        if self.lifted_at.tzinfo is None:
            msg = "lifted_at must be tz-aware UTC"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class GreeksRefreshFailedDetail:
    """Detail payload for ``GREEKS_REFRESH_FAILED`` events.

    Emitted by the continuous monitor's greeks-refresh task (story 03a) when
    the IV-fetch retry budget is exhausted for an open option / strategy
    position. The position's prior greeks are preserved untouched; the
    ``OptionGreeks.refresh_failed`` flag flips to ``True`` so downstream
    consumers (breach evaluation, bracket-stop firing) widen their derivation
    uncertainty buffer per
    ``docs/design/05-execution-layer/architecture.md`` § 4d.

    ``failure_reason`` is a short identifier suitable for log queries
    (``"iv_fetch_timeout"``, ``"iv_fetch_404"``, ``"iv_fetch_db_error"``,
    ``"iv_fetch_no_row"``). ``prior_as_of`` is the ``as_of_timestamp`` of
    the now-preserved greeks; ``None`` when the position has never been
    successfully refreshed (the first cycle after open observed the failure).
    """

    underlying_ticker: str
    occ_symbol: str
    failure_reason: str
    prior_as_of: datetime | None


@dataclass(frozen=True, slots=True)
class EmergencyInvocationRequestedDetail:
    """Detail payload for ``EMERGENCY_INVOCATION_REQUESTED`` events.

    Emitted by the continuous monitor when one of the four emergency-invocation
    triggers (regime jump, multi-rule breach, drawdown velocity, margin call)
    fires per ``docs/design/06-risk-guardrails/breach-behavior.md`` § Emergency
    invocation trigger. The pipeline scheduler's emergency receiver task
    consumes the entry, enforces the 30-minute cooldown (with margin-call
    override), and dispatches one ``run_invocation`` with
    ``trigger_type="emergency"`` and ``firing_run_type=RunType.emergency``.

    ``cooldown_remaining_seconds`` is the writer-side observation at emission
    time (0 when cooldown is satisfied or bypassed by ``margin_call``). The
    receiver re-checks the cooldown against the live ``invocations`` table.
    """

    trigger_type: Literal["regime_jump", "multi_rule_breach", "drawdown_velocity", "margin_call"]
    trigger_reason: str
    cooldown_remaining_seconds: int

    def __post_init__(self) -> None:
        if self.cooldown_remaining_seconds < 0:
            msg = (
                "cooldown_remaining_seconds must be non-negative; got "
                f"{self.cooldown_remaining_seconds}"
            )
            raise ValueError(msg)


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (
        EventType.GUARDRAIL_REJECTION,
        GuardrailRejectionDetail,
        EventGroup.RISK_AND_GUARDRAIL,
    ),
    (
        EventType.RISK_LIMIT_APPROACHED,
        RiskLimitApproachedDetail,
        EventGroup.RISK_AND_GUARDRAIL,
    ),
    (
        EventType.RISK_PARAMETER_CHANGED,
        RiskParameterChangedDetail,
        EventGroup.RISK_AND_GUARDRAIL,
    ),
    (
        EventType.EMERGENCY_INVOCATION_REQUESTED,
        EmergencyInvocationRequestedDetail,
        EventGroup.RISK_AND_GUARDRAIL,
    ),
    (
        EventType.HALT_ACTIVATED,
        HaltActivatedDetail,
        EventGroup.RISK_AND_GUARDRAIL,
    ),
    (
        EventType.HALT_LIFTED,
        HaltLiftedDetail,
        EventGroup.RISK_AND_GUARDRAIL,
    ),
    (
        EventType.GREEKS_REFRESH_FAILED,
        GreeksRefreshFailedDetail,
        EventGroup.RISK_AND_GUARDRAIL,
    ),
]


__all__ = [
    "EmergencyInvocationRequestedDetail",
    "GreeksRefreshFailedDetail",
    "GuardrailRejectionDetail",
    "HaltActivatedDetail",
    "HaltLiftedDetail",
    "RiskLimitApproachedDetail",
    "RiskParameterChangedDetail",
]
