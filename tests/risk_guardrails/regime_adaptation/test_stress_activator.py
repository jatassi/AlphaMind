"""Tests for ``stress_activator`` — story 06b.

Two surfaces under test:

- ``evaluate_stress_overlay`` — pure activation function with no I/O.
- ``fetch_composite_alert_state`` — thin DB-read helper that selects the
  most-recent ``DistillationCompositeState`` row per composite kind and
  packages the four scalar inputs ``evaluate_stress_overlay`` needs.

The activation contract — funding-stress composite alert OR market-liquidity
score alert tightens exposure-related rule limits by 15% — comes from
``docs/design/06-risk-guardrails/regime-adaptation.md`` § Distillation layer
anomaly alerts. Calibration-state suppression (BOOTSTRAP / UNAVAILABLE
alerts do not activate) is the conservative-tightening discipline documented
in the story file's Notes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.config.models.overlays import Overlay, StressOverlay
from alphamind.distillation.calibration import CalibrationState
from alphamind.persistence.models import Base, DistillationCompositeState
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.risk_guardrails.regime_adaptation import (
    CompositeAlertState,
    OverlayActivationDecision,
)
from alphamind.risk_guardrails.regime_adaptation.stress_activator import (
    evaluate_stress_overlay,
    fetch_composite_alert_state,
)


def test_top_level_package_reexports_activator_surface() -> None:
    """Story 06b acceptance criterion: ``evaluate_stress_overlay`` and
    ``fetch_composite_alert_state`` are re-exported from the package
    ``__init__.py`` so callers can import them via the top-level path.
    """
    import alphamind.risk_guardrails.regime_adaptation as regime_adaptation

    assert regime_adaptation.evaluate_stress_overlay is evaluate_stress_overlay
    assert regime_adaptation.fetch_composite_alert_state is fetch_composite_alert_state


# ---------------------------------------------------------------------------
# Stress-overlay config builder
# ---------------------------------------------------------------------------


def _stress_overlay(
    triggers: list[str] | None = None,
) -> StressOverlay:
    """Build a stress overlay with the production triggers (or the supplied subset)."""
    payload: dict[str, Any] = {
        "activation": {
            "triggers": triggers
            if triggers is not None
            else ["funding_stress_composite", "market_liquidity_score"],
        },
        "multipliers": {
            "sector_concentration_pct": 0.85,
            "net_long_pct": 0.85,
            "net_short_pct": 0.85,
            "gross_exposure_pct": 0.85,
        },
    }
    return StressOverlay.model_validate(payload)


# ---------------------------------------------------------------------------
# Database fixtures (used by fetch_composite_alert_state tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def _composite_row(
    composite_kind: str,
    as_of: str,
    *,
    alert_active: int,
    calibration_state: str = "calibrated",
) -> DistillationCompositeState:
    return DistillationCompositeState(
        composite_kind=composite_kind,
        as_of=as_of,
        composite_value=1.0,
        component_breakdown_json="{}",
        percentile_60d=0.0,
        alert_active=alert_active,
        calibration_state=calibration_state,
        ingested_at="2026-04-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# evaluate_stress_overlay — activation rules
# ---------------------------------------------------------------------------


class TestEvaluateStressOverlayNoAlerts:
    """Both alert flags False, both calibrated → overlay inactive."""

    def test_returns_inactive_decision_for_overlay_stress(self) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=False,
            market_liquidity_alert_active=False,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=CalibrationState.CALIBRATED,
            stress_overlay=_stress_overlay(),
        )
        assert isinstance(decision, OverlayActivationDecision)
        assert decision.overlay == Overlay.stress
        assert decision.is_active is False
        assert decision.pre_event_block_new_positions is False
        assert decision.rationale == ""


class TestEvaluateStressOverlayFundingStressOnly:
    """Funding-stress calibrated alert active, market-liquidity calm → active."""

    def test_activates_with_funding_stress_calibrated_rationale(self) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=True,
            market_liquidity_alert_active=False,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=CalibrationState.CALIBRATED,
            stress_overlay=_stress_overlay(),
        )
        assert decision.overlay == Overlay.stress
        assert decision.is_active is True
        assert decision.pre_event_block_new_positions is False
        assert "funding_stress alert (CALIBRATED)" in decision.rationale
        assert "market_liquidity" not in decision.rationale


class TestEvaluateStressOverlayMarketLiquidityOnly:
    """Market-liquidity calibrated alert active, funding-stress calm → active."""

    def test_activates_with_market_liquidity_calibrated_rationale(self) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=False,
            market_liquidity_alert_active=True,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=CalibrationState.CALIBRATED,
            stress_overlay=_stress_overlay(),
        )
        assert decision.overlay == Overlay.stress
        assert decision.is_active is True
        assert decision.pre_event_block_new_positions is False
        assert "market_liquidity alert (CALIBRATED)" in decision.rationale
        assert "funding_stress" not in decision.rationale


class TestEvaluateStressOverlayBothCalibrated:
    """Both calibrated alerts → active, rationale lists both."""

    def test_activates_with_both_alerts_in_rationale(self) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=True,
            market_liquidity_alert_active=True,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=CalibrationState.CALIBRATED,
            stress_overlay=_stress_overlay(),
        )
        assert decision.is_active is True
        assert "funding_stress alert (CALIBRATED)" in decision.rationale
        assert "market_liquidity alert (CALIBRATED)" in decision.rationale


class TestEvaluateStressOverlayUncalibrated:
    """Single alert with BOOTSTRAP / UNAVAILABLE calibration does not activate."""

    @pytest.mark.parametrize(
        "calibration_state",
        [CalibrationState.BOOTSTRAP, CalibrationState.UNAVAILABLE],
    )
    def test_funding_stress_alert_with_uncalibrated_state_suppressed(
        self,
        calibration_state: CalibrationState,
    ) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=True,
            market_liquidity_alert_active=False,
            funding_stress_calibration_state=calibration_state,
            market_liquidity_calibration_state=CalibrationState.CALIBRATED,
            stress_overlay=_stress_overlay(),
        )
        assert decision.is_active is False
        assert decision.pre_event_block_new_positions is False
        assert "calibration insufficient" in decision.rationale
        assert f"funding_stress: {calibration_state.name}" in decision.rationale

    @pytest.mark.parametrize(
        "calibration_state",
        [CalibrationState.BOOTSTRAP, CalibrationState.UNAVAILABLE],
    )
    def test_market_liquidity_alert_with_uncalibrated_state_suppressed(
        self,
        calibration_state: CalibrationState,
    ) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=False,
            market_liquidity_alert_active=True,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=calibration_state,
            stress_overlay=_stress_overlay(),
        )
        assert decision.is_active is False
        assert "calibration insufficient" in decision.rationale
        assert f"market_liquidity: {calibration_state.name}" in decision.rationale


class TestEvaluateStressOverlayMixedCalibration:
    """Calibrated alert + uncalibrated alert: activation succeeds on the calibrated."""

    def test_calibrated_funding_overrides_bootstrap_market(self) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=True,
            market_liquidity_alert_active=True,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=CalibrationState.BOOTSTRAP,
            stress_overlay=_stress_overlay(),
        )
        assert decision.is_active is True
        assert "funding_stress alert (CALIBRATED)" in decision.rationale
        assert "market_liquidity alert" not in decision.rationale


class TestEvaluateStressOverlayTriggerSubset:
    """A trigger absent from ``stress_overlay.activation.triggers`` does not contribute."""

    def test_funding_only_trigger_ignores_market_liquidity_alert(self) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=False,
            market_liquidity_alert_active=True,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=CalibrationState.CALIBRATED,
            stress_overlay=_stress_overlay(triggers=["funding_stress_composite"]),
        )
        assert decision.is_active is False
        assert decision.rationale == ""

    def test_market_liquidity_only_trigger_ignores_funding_stress_alert(self) -> None:
        decision = evaluate_stress_overlay(
            funding_stress_alert_active=True,
            market_liquidity_alert_active=False,
            funding_stress_calibration_state=CalibrationState.CALIBRATED,
            market_liquidity_calibration_state=CalibrationState.CALIBRATED,
            stress_overlay=_stress_overlay(triggers=["market_liquidity_score"]),
        )
        assert decision.is_active is False
        assert decision.rationale == ""


class TestEvaluateStressOverlayDeterminism:
    """Identical inputs produce identical outputs."""

    def test_repeated_calls_produce_equal_decisions(self) -> None:
        kwargs: dict[str, Any] = {
            "funding_stress_alert_active": True,
            "market_liquidity_alert_active": False,
            "funding_stress_calibration_state": CalibrationState.CALIBRATED,
            "market_liquidity_calibration_state": CalibrationState.UNAVAILABLE,
            "stress_overlay": _stress_overlay(),
        }
        first = evaluate_stress_overlay(**kwargs)
        second = evaluate_stress_overlay(**kwargs)
        assert first == second


# ---------------------------------------------------------------------------
# fetch_composite_alert_state — DB-read helper
# ---------------------------------------------------------------------------


class TestFetchCompositeAlertStateEmptyTable:
    """Empty table → both kinds report (False, UNAVAILABLE, None)."""

    def test_returns_unavailable_for_both_kinds(self, session: Session) -> None:
        state = fetch_composite_alert_state(session)
        assert isinstance(state, CompositeAlertState)
        assert state.funding_stress_alert_active is False
        assert state.funding_stress_calibration_state == CalibrationState.UNAVAILABLE
        assert state.funding_stress_as_of is None
        assert state.market_liquidity_alert_active is False
        assert state.market_liquidity_calibration_state == CalibrationState.UNAVAILABLE
        assert state.market_liquidity_as_of is None


class TestFetchCompositeAlertStateHappyPath:
    """One row per kind → fields mirror the persisted row."""

    def test_reads_alert_flag_calibration_and_as_of_per_kind(self, session: Session) -> None:
        session.add(
            _composite_row(
                "funding_stress",
                "2026-04-15T12:00:00Z",
                alert_active=1,
                calibration_state="calibrated",
            )
        )
        session.add(
            _composite_row(
                "market_liquidity",
                "2026-04-15T12:00:00Z",
                alert_active=0,
                calibration_state="bootstrap",
            )
        )
        session.flush()

        state = fetch_composite_alert_state(session)

        assert state.funding_stress_alert_active is True
        assert state.funding_stress_calibration_state == CalibrationState.CALIBRATED
        assert state.funding_stress_as_of == "2026-04-15T12:00:00Z"
        assert state.market_liquidity_alert_active is False
        assert state.market_liquidity_calibration_state == CalibrationState.BOOTSTRAP
        assert state.market_liquidity_as_of == "2026-04-15T12:00:00Z"


class TestFetchCompositeAlertStateMostRecentRow:
    """Multiple rows per kind → helper returns the most-recent."""

    def test_funding_stress_returns_latest_as_of(self, session: Session) -> None:
        session.add(
            _composite_row(
                "funding_stress",
                "2026-04-14T12:00:00Z",
                alert_active=0,
                calibration_state="calibrated",
            )
        )
        session.add(
            _composite_row(
                "funding_stress",
                "2026-04-15T12:00:00Z",
                alert_active=1,
                calibration_state="calibrated",
            )
        )
        session.flush()

        state = fetch_composite_alert_state(session)

        assert state.funding_stress_alert_active is True
        assert state.funding_stress_as_of == "2026-04-15T12:00:00Z"


class TestFetchCompositeAlertStateMissingComposite:
    """Only one composite kind has rows → the other reports UNAVAILABLE."""

    def test_only_funding_stress_present(self, session: Session) -> None:
        session.add(
            _composite_row(
                "funding_stress",
                "2026-04-15T12:00:00Z",
                alert_active=1,
                calibration_state="calibrated",
            )
        )
        session.flush()

        state = fetch_composite_alert_state(session)

        assert state.funding_stress_alert_active is True
        assert state.funding_stress_calibration_state == CalibrationState.CALIBRATED
        assert state.funding_stress_as_of == "2026-04-15T12:00:00Z"
        assert state.market_liquidity_alert_active is False
        assert state.market_liquidity_calibration_state == CalibrationState.UNAVAILABLE
        assert state.market_liquidity_as_of is None
