"""Stress overlay activator (story 06b).

The activator decides whether the ``stress`` overlay is active for the
current invocation based on the most-recent ``DistillationCompositeState``
``alert_active`` flag for each of ``funding_stress`` and ``market_liquidity``.

Two surfaces:

- ``evaluate_stress_overlay`` — pure function. Inputs are four scalars (the
  two alert flags + their calibration states) plus the overlay config;
  output is an ``OverlayActivationDecision``.
- ``fetch_composite_alert_state`` — thin DB-read helper that selects the
  most-recent row per composite kind and packages the four scalars the
  pure function consumes. The orchestrator (story 09) calls the helper
  once at invocation start and then passes the resulting record's fields
  to ``evaluate_stress_overlay``.

The duplication of names across the distillation and overlay layers
(``funding_stress`` ↔ ``funding_stress_composite``, ``market_liquidity`` ↔
``market_liquidity_score``) is bridged here via
``_COMPOSITE_KIND_TO_OVERLAY_TRIGGER`` — the only place either convention
is referenced together. See the story file's Notes section for rationale.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.overlays import Overlay, StressOverlay, StressTrigger
from alphamind.distillation.calibration import CalibrationState
from alphamind.risk_guardrails.regime_adaptation.types import (
    CompositeAlertState,
    OverlayActivationDecision,
)

if TYPE_CHECKING:
    # Eager import would re-enter ``persistence.models`` mid-load (it
    # imports ``RegimeTransitionState`` from this package). The DB-read
    # helper imports lazily below.
    from alphamind.persistence.models import DistillationCompositeState

# ---------------------------------------------------------------------------
# Composite-kind ↔ overlay-trigger bridge
# ---------------------------------------------------------------------------

_COMPOSITE_KIND_TO_OVERLAY_TRIGGER: Mapping[str, StressTrigger] = MappingProxyType(
    {
        "funding_stress": StressTrigger.funding_stress_composite,
        "market_liquidity": StressTrigger.market_liquidity_score,
    }
)


# ---------------------------------------------------------------------------
# Pure activator
# ---------------------------------------------------------------------------


def evaluate_stress_overlay(
    *,
    funding_stress_alert_active: bool,
    market_liquidity_alert_active: bool,
    funding_stress_calibration_state: CalibrationState,
    market_liquidity_calibration_state: CalibrationState,
    stress_overlay: StressOverlay,
) -> OverlayActivationDecision:
    """Decide whether the stress overlay is active for this invocation.

    See ``docs/implementation/06-risk-guardrails/regime-adaptation/06b-stress-overlay-activator.md``
    for the full activation contract.
    """
    enabled_triggers = frozenset(stress_overlay.activation.triggers)

    funding_stress_contributes = (
        funding_stress_alert_active
        and funding_stress_calibration_state == CalibrationState.CALIBRATED
        and _COMPOSITE_KIND_TO_OVERLAY_TRIGGER["funding_stress"] in enabled_triggers
    )
    market_liquidity_contributes = (
        market_liquidity_alert_active
        and market_liquidity_calibration_state == CalibrationState.CALIBRATED
        and _COMPOSITE_KIND_TO_OVERLAY_TRIGGER["market_liquidity"] in enabled_triggers
    )

    is_active = funding_stress_contributes or market_liquidity_contributes
    rationale = _compute_rationale(
        funding_stress_alert_active=funding_stress_alert_active,
        market_liquidity_alert_active=market_liquidity_alert_active,
        funding_stress_calibration_state=funding_stress_calibration_state,
        market_liquidity_calibration_state=market_liquidity_calibration_state,
        activation_decision=is_active,
    )

    return OverlayActivationDecision(
        overlay=Overlay.stress,
        is_active=is_active,
        rationale=rationale,
        pre_event_block_new_positions=False,
    )


def _compute_rationale(
    *,
    funding_stress_alert_active: bool,
    market_liquidity_alert_active: bool,
    funding_stress_calibration_state: CalibrationState,
    market_liquidity_calibration_state: CalibrationState,
    activation_decision: bool,
) -> str:
    """Render the human-readable rationale for the overlay decision.

    Three shapes:

    - ``activation_decision=True``: list every calibrated active alert by
      composite kind followed by ``(CALIBRATED)``. Multiple contributors are
      comma-separated.
    - ``activation_decision=False`` and at least one alert is True but
      uncalibrated: surface the suppression as ``"Stress signals present but
      calibration insufficient (...)"``.
    - ``activation_decision=False`` and no alert is True: empty string,
      mirroring the no-activation convention used by the pre-event activator.
    """
    if activation_decision:
        contributors: list[str] = []
        if (
            funding_stress_alert_active
            and funding_stress_calibration_state == CalibrationState.CALIBRATED
        ):
            contributors.append(f"funding_stress alert ({funding_stress_calibration_state.name})")
        if (
            market_liquidity_alert_active
            and market_liquidity_calibration_state == CalibrationState.CALIBRATED
        ):
            contributors.append(
                f"market_liquidity alert ({market_liquidity_calibration_state.name})"
            )
        return "Stress overlay active: " + ", ".join(contributors)

    has_uncalibrated_alert = (
        funding_stress_alert_active
        and funding_stress_calibration_state != CalibrationState.CALIBRATED
    ) or (
        market_liquidity_alert_active
        and market_liquidity_calibration_state != CalibrationState.CALIBRATED
    )
    if has_uncalibrated_alert:
        return (
            "Stress signals present but calibration insufficient "
            f"(funding_stress: {funding_stress_calibration_state.name}, "
            f"market_liquidity: {market_liquidity_calibration_state.name})"
        )

    return ""


# ---------------------------------------------------------------------------
# Database-read helper
# ---------------------------------------------------------------------------


def fetch_composite_alert_state(session: Session) -> CompositeAlertState:
    """Read the most-recent ``DistillationCompositeState`` row for each
    composite kind and pack the four scalar inputs that
    ``evaluate_stress_overlay`` consumes.

    Missing composite kinds are reported as
    ``alert_active=False, calibration_state=UNAVAILABLE, as_of=None``;
    ``evaluate_stress_overlay`` then naturally suppresses activation on
    that kind.
    """
    funding_stress_row = _latest_composite_row(session, "funding_stress")
    market_liquidity_row = _latest_composite_row(session, "market_liquidity")

    funding_active, funding_state, funding_as_of = _row_to_scalars(funding_stress_row)
    liquidity_active, liquidity_state, liquidity_as_of = _row_to_scalars(market_liquidity_row)

    return CompositeAlertState(
        funding_stress_alert_active=funding_active,
        funding_stress_calibration_state=funding_state,
        funding_stress_as_of=funding_as_of,
        market_liquidity_alert_active=liquidity_active,
        market_liquidity_calibration_state=liquidity_state,
        market_liquidity_as_of=liquidity_as_of,
    )


def _latest_composite_row(
    session: Session,
    composite_kind: str,
) -> DistillationCompositeState | None:
    # Lazy import — see TYPE_CHECKING block at top of module for the cycle rationale.
    from alphamind.persistence.models import DistillationCompositeState

    statement = (
        select(DistillationCompositeState)
        .where(DistillationCompositeState.composite_kind == composite_kind)
        .order_by(DistillationCompositeState.as_of.desc())
        .limit(1)
    )
    return session.execute(statement).scalar_one_or_none()


def _row_to_scalars(
    row: DistillationCompositeState | None,
) -> tuple[bool, CalibrationState, str | None]:
    if row is None:
        return False, CalibrationState.UNAVAILABLE, None
    return bool(row.alert_active), CalibrationState(row.calibration_state), row.as_of
