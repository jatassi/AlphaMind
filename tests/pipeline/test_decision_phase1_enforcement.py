"""Tests for the decision pipeline's Phase 1 enforcement composition wiring — story ALP-433.

The decision pipeline composes the canonical Phase 1 enforcement output via
``compose_phase_1_enforcement`` + ``make_active_risk_parameters_provider`` so
callers no longer pass a pre-built provider. The pipeline also surfaces the
classified ``drawdown_tier`` on the bundled result.

Tests focus on the new behaviors: the helper signature, the composition
result on the bundled pipeline result, and the override/no-override paths.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier

# ---------------------------------------------------------------------------
# Fixture progressive tiers — mirror config/guardrails.yaml
# ---------------------------------------------------------------------------


def _progressive_tiers() -> tuple[ProgressiveTier, ...]:
    """Three-tier ladder mirroring ``config/guardrails.yaml`` — tier 1 @ 8%
    (CONSTRAINED), tier 2 @ 10% (HEAVILY_CONSTRAINED), tier 3 @ 12% (FULL_HALT)."""
    return (
        ProgressiveTier.model_validate(
            {"trigger_pct": 8.0, "max_position_size_pct": 3.0, "max_gross_pct": 80.0}
        ),
        ProgressiveTier.model_validate(
            {"trigger_pct": 10.0, "max_position_size_pct": 2.0, "max_gross_pct": 60.0}
        ),
        ProgressiveTier.model_validate({"trigger_pct": 12.0, "full_halt": True}),
    )


def _drawdown_state(*, current_drawdown_pct: float = 0.0) -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": current_drawdown_pct,
            "equity_high_water_mark_usd": 100_000.0,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": current_drawdown_pct,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


def _regime_resolved_parameters() -> ActiveRiskParameterSet:
    """Baseline regime-resolved parameter set carrying the two rule-ids the
    progressive-tier overrider edits, so override tests can verify the rewrite.
    """
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Per-position max size",
                value=5.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
            ActiveRiskParameterEntry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                value=100.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=100.0,
            ),
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown limit",
                value=2.5,
                unit="% of equity",
                regime_multiplier_applied=1.0,
                base_value=2.5,
            ),
            ActiveRiskParameterEntry(
                rule_id="cumulative_drawdown_pct",
                rule_label="Cumulative drawdown",
                value=10.0,
                unit="% of equity",
                regime_multiplier_applied=1.0,
                base_value=10.0,
            ),
        ),
        active_overlays=(),
    )


def _regime_output(parameters: ActiveRiskParameterSet) -> Any:
    """Build a synthetic ``RegimeAdaptationOutput`` wrapping *parameters*
    as the regime-resolved active-risk parameter set."""
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        RegimeAdaptationOutput,
        RegimeAdaptationState,
    )

    state = RegimeAdaptationState(
        as_of=datetime(2026, 5, 10, 14, 30, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        invocation_id="inv-test-001",
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="normal",
        distillation_vix_level=18.0,
        regime_skip_emergency=False,
    )
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=parameters,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=state,
        audit_log_entries=(),
    )


# ---------------------------------------------------------------------------
# Helper: build_phase1_enforcement_inputs is exported and async
# ---------------------------------------------------------------------------


def test_build_phase1_enforcement_inputs_is_async_and_exported() -> None:
    """The helper lives in ``alphamind.pipeline._shared`` and is an async function."""
    from alphamind.pipeline import _shared

    assert "build_phase1_enforcement_inputs" in _shared.__all__
    helper = _shared.build_phase1_enforcement_inputs
    assert inspect.iscoroutinefunction(helper)


def test_build_phase1_enforcement_inputs_reads_drawdown_from_repository() -> None:
    """The helper calls ``repository.get_drawdown_state()`` and threads its
    return value into the tuple."""
    from alphamind.pipeline._shared import build_phase1_enforcement_inputs

    expected_drawdown = _drawdown_state(current_drawdown_pct=8.5)

    class _Repo:
        async def get_drawdown_state(self) -> DrawdownState:
            return expected_drawdown

    regime_output = _regime_output(_regime_resolved_parameters())
    progressive_tiers = _progressive_tiers()

    # The minimal stub only exposes ``get_drawdown_state`` — the helper
    # only needs that one method, but ``PortfolioStateRepository`` is a
    # full Protocol with ~17 methods. Stubbing the rest is unwarranted
    # ceremony for a unit-scoped helper test; suppress the arg-type
    # narrowing instead.
    result = asyncio.run(
        build_phase1_enforcement_inputs(
            repository=_Repo(),  # type: ignore[arg-type]
            regime_output=regime_output,
            progressive_tiers=progressive_tiers,
        )
    )

    # The helper returns a 3-tuple suitable for unpacking into
    # ``compose_phase_1_enforcement(**dict(zip([...], result)))``.
    assert isinstance(result, tuple)
    assert len(result) == 3
    returned_regime, returned_drawdown, returned_tiers = result
    assert returned_regime is regime_output
    assert returned_drawdown is expected_drawdown
    assert returned_tiers is progressive_tiers


# ---------------------------------------------------------------------------
# DecisionPipelineResult: drawdown_tier surfaced on bundled result
# ---------------------------------------------------------------------------


def test_decision_pipeline_result_carries_drawdown_tier_field() -> None:
    """``DecisionPipelineResult`` exposes ``drawdown_tier: DrawdownTier | None``."""
    from alphamind.pipeline.decision import DecisionPipelineResult

    fields = {f.name for f in __import__("dataclasses").fields(DecisionPipelineResult)}
    assert "drawdown_tier" in fields


# ---------------------------------------------------------------------------
# Pipeline integration tests — exercise the runner end-to-end with stubs.
# ---------------------------------------------------------------------------


def _build_pipeline_kwargs(
    *,
    drawdown_pct: float,
    progressive_tiers: tuple[ProgressiveTier, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Build the kwarg dict for ``run_decision_pipeline`` with a customizable drawdown.

    Reuses the test_decision module's stub-runner infrastructure for the
    four agent slots; the new kwargs (repository, regime_output,
    progressive_tiers) supply Phase 1 enforcement inputs.
    """
    from tests.pipeline.test_decision import (
        _CallLog,
        _make_minimal_inputs,
        _patch_runners,
    )

    log = _CallLog()
    _patch_runners(monkeypatch, log=log)

    kwargs = asyncio.run(_make_minimal_inputs())
    parameters = _regime_resolved_parameters()
    regime_output = _regime_output(parameters)

    # Build a stub repository that returns the configured drawdown.
    state = _drawdown_state(current_drawdown_pct=drawdown_pct)

    class _Repo:
        async def get_drawdown_state(self) -> DrawdownState:
            return state

    kwargs["repository"] = _Repo()
    kwargs["regime_output"] = regime_output
    kwargs["progressive_tiers"] = progressive_tiers
    return kwargs


def test_no_drawdown_yields_regime_resolved_parameters_and_none_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """drawdown 0.0 → ``result.drawdown_tier is None`` and parameters pass through."""
    from alphamind.pipeline.decision import run_decision_pipeline

    progressive_tiers = _progressive_tiers()
    kwargs = _build_pipeline_kwargs(
        drawdown_pct=0.0, progressive_tiers=progressive_tiers, monkeypatch=monkeypatch
    )

    result = asyncio.run(run_decision_pipeline(**kwargs))

    assert result.drawdown_tier is None
    # Snapshot's active_risk_parameters carry no progressive-tier overlay.
    overlays = result.pydantic_snapshot.active_risk_parameters.active_overlays
    assert not any(tag.startswith("cumulative_drawdown_tier_") for tag in overlays)


def test_tier_1_drawdown_applies_constrained_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """drawdown clears tier 1 → ``drawdown_tier == CONSTRAINED`` and overrides applied."""
    from alphamind.pipeline.decision import run_decision_pipeline

    progressive_tiers = _progressive_tiers()
    # 8.5% > tier 1 trigger (8%); below tier 2 trigger (10%).
    kwargs = _build_pipeline_kwargs(
        drawdown_pct=8.5, progressive_tiers=progressive_tiers, monkeypatch=monkeypatch
    )

    result = asyncio.run(run_decision_pipeline(**kwargs))

    assert result.drawdown_tier == DrawdownTier.CONSTRAINED
    overlays = result.pydantic_snapshot.active_risk_parameters.active_overlays
    assert "cumulative_drawdown_tier_1" in overlays
    entries_by_id = {e.rule_id: e for e in result.pydantic_snapshot.active_risk_parameters.entries}
    # tier 1 caps: max_position_size_pct=3.0, max_gross_pct=80.0 — both tighter
    # than the regime-resolved 5.0 / 100.0 so the min() wins.
    assert entries_by_id["position_max_size_pct"].value == pytest.approx(3.0)
    assert entries_by_id["gross_exposure_pct"].value == pytest.approx(80.0)


def test_pipeline_invocation_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identical inputs produce identical phase1 enforcement composition."""
    from alphamind.pipeline.decision import run_decision_pipeline

    progressive_tiers = _progressive_tiers()

    # Two independent kwarg builds (fresh monkeypatch per run) — both deterministic.
    kwargs_1 = _build_pipeline_kwargs(
        drawdown_pct=8.5, progressive_tiers=progressive_tiers, monkeypatch=monkeypatch
    )
    result_1 = asyncio.run(run_decision_pipeline(**kwargs_1))

    kwargs_2 = _build_pipeline_kwargs(
        drawdown_pct=8.5, progressive_tiers=progressive_tiers, monkeypatch=monkeypatch
    )
    result_2 = asyncio.run(run_decision_pipeline(**kwargs_2))

    assert result_1.drawdown_tier == result_2.drawdown_tier
    entries_1 = {
        e.rule_id: e.value for e in result_1.pydantic_snapshot.active_risk_parameters.entries
    }
    entries_2 = {
        e.rule_id: e.value for e in result_2.pydantic_snapshot.active_risk_parameters.entries
    }
    assert entries_1 == entries_2
