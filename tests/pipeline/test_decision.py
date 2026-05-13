"""Tests for the decision-layer pipeline composition runner — story ALP-403.

The composition runner mirrors :mod:`alphamind.pipeline.analysis`: it
sequences ``assemble_snapshot`` → ``to_library_snapshot`` → consumer view
projections → (``run_analyst`` + ``run_strategist``) parallel →
``run_proposal_pre_processor`` → ``run_portfolio_manager`` and returns every
typed ``*Result``. The four agent runners and the assembler are
monkeypatched at the composition module's namespace so the tests do not
touch SQLite, the filesystem, or the Anthropic SDK.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from alphamind._kernel.ids import (
    InvocationId,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName, AllowedModel, BaseAgentConfig
from alphamind.decision.analyst.harness import SDKFailure
from alphamind.decision.analyst.runner import AnalystResult
from alphamind.decision.portfolio_manager.runner import PMResult
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.decision.strategist.runner import StrategistResult
from alphamind.pipeline import _shared
from alphamind.pipeline._shared import apply_agent_overrides
from alphamind.pipeline.analysis import apply_agent_overrides as analysis_apply_overrides
from alphamind.portfolio_state.pricing import PriceQuote, StubCurrentPriceProvider

# ---------------------------------------------------------------------------
# Hoist contract
# ---------------------------------------------------------------------------


def test_apply_agent_overrides_re_exported_by_analysis_module() -> None:
    """The analysis pipeline still exposes ``apply_agent_overrides`` under
    its old import path so existing callers do not break."""
    assert analysis_apply_overrides is apply_agent_overrides


def test_apply_agent_overrides_layers_overrides() -> None:
    """The hoisted helper layers per-trigger override fields onto base
    configs via Pydantic ``model_copy``."""
    base = BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/analyst.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=[],
    )
    agents = {AgentName.analyst: base}
    overrides = {AgentName.analyst: {"output_token_budget": 9_000}}
    resolved = apply_agent_overrides(agents, overrides)
    cfg = resolved[AgentName.analyst.value]
    assert cfg.output_token_budget == 9_000
    # Untouched fields preserve base value
    assert cfg.latency_budget_seconds == 30


def test_shared_module_advertises_apply_agent_overrides_in_all() -> None:
    """``__all__`` advertises the public symbol for re-export."""
    assert "apply_agent_overrides" in _shared.__all__


# ---------------------------------------------------------------------------
# Builders for the decision-pipeline composition runner
# ---------------------------------------------------------------------------


_INVOCATION_ID = "inv-decision-001"
_AS_OF = datetime(2026, 5, 10, 14, 30, tzinfo=UTC)
_NOW = _AS_OF
_TIMESTAMP = _AS_OF


def _pipeline_inputs_from_fixture(
    fixture: Any,
    quotes: dict[str, PriceQuote],
    config: Any,
    now: datetime,
    *,
    active_sectors: tuple[str, ...],
) -> dict[str, Any]:
    """Build the ``run_decision_pipeline`` kwarg dict for any fixture tuple.

    Assembles a real :class:`AssembledSnapshot` from the supplied fixture
    (via :func:`assemble_snapshot` against a stub repository) so the
    composition runner receives the same pre-built snapshot shape the
    orchestrator produces in production (ALP-449 Slice 2). Then composes
    the library config, market inputs, and state-delivery config around
    the caller-supplied ``active_sectors`` tuple.
    """
    from types import MappingProxyType

    from alphamind._kernel.regime import (
        RegimeLabel,
        RegimeTransitionState,
    )
    from alphamind.portfolio_state.aggregates.risk_parameters import (
        ActiveRiskParameterEntry,
        ActiveRiskParameterSet,
    )
    from alphamind.portfolio_state.assembler import assemble_snapshot
    from alphamind.portfolio_state.repository import StubPortfolioStateRepository
    from alphamind.risk_guardrails.guardrail_evaluation import (
        FeatureFlagsView,
        FixtureIvProvider,
        LibraryConfig,
        MarketInputs,
    )
    from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

    active_risk_parameters = ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Per-position max size",
                value=10.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=10.0,
            ),
            ActiveRiskParameterEntry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
                value=200.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=200.0,
            ),
        ),
        active_overlays=(),
    )
    fixture = fixture.model_copy(update={"active_risk_parameters": active_risk_parameters})

    from alphamind.portfolio_state.consumers.synthesizer import adapt_ticker_sector_resolver

    repository = StubPortfolioStateRepository(fixture)
    price_provider = StubCurrentPriceProvider(quotes, now)
    assembled = assemble_snapshot(
        repository=repository,
        price_provider=price_provider,
        sector_resolver=adapt_ticker_sector_resolver(_sector_resolver),
        config=config,
        now=now,
    )
    feature_flags = FeatureFlagsView(options_enabled=False, short_selling_enabled=False)
    library_config = LibraryConfig(
        effective_limits=MappingProxyType({"position_max_size_pct": 10.0}),
        escalation_zones=MappingProxyType({}),
        feature_flags=feature_flags,
        active_sectors=active_sectors,
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )
    library_market = MarketInputs(
        underlying_prices=MappingProxyType({}),
        risk_free_rate=0.045,
        iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        as_of=now,
    )
    state_delivery_config = StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
    )

    return {
        "assembled_snapshot": assembled,
        "repository": repository,
        "regime_output": _build_regime_output(active_risk_parameters),
        "progressive_tiers": _progressive_tiers_fixture(),
        "synthesizer_text": "Synthesizer brief.",
        "retrieval_store": RetrievalStore(entries={}, freshness_by_source={}),
        "mode": "normal",
        "halt_state": None,
        "agents_config": _agents_registry(),
        "agent_overrides": {},
        "sector_resolver": _sector_resolver,
        "borrow_cost_resolver": None,
        "library_config": library_config,
        "library_market": library_market,
        "profile_feature_flags": feature_flags,
        "state_delivery_config": state_delivery_config,
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": frozenset(active_sectors),
        "invocation_id": _INVOCATION_ID,
        "timestamp": now,
    }


def _progressive_tiers_fixture() -> tuple[Any, ...]:
    """Three-tier ladder mirroring ``config/guardrails.yaml``."""
    from alphamind.config.models.guardrails import ProgressiveTier

    return (
        ProgressiveTier.model_validate(
            {"trigger_pct": 8.0, "max_position_size_pct": 3.0, "max_gross_pct": 80.0}
        ),
        ProgressiveTier.model_validate(
            {"trigger_pct": 10.0, "max_position_size_pct": 2.0, "max_gross_pct": 60.0}
        ),
        ProgressiveTier.model_validate({"trigger_pct": 12.0, "full_halt": True}),
    )


def _build_regime_output(parameters: Any) -> Any:
    """Build a fixture ``RegimeAdaptationOutput`` carrying *parameters*."""
    from alphamind._kernel.regime import RegimeTransitionState
    from alphamind.config.models.regimes import Regime
    from alphamind.risk_guardrails.regime_adaptation import (
        RegimeAdaptationOutput,
        RegimeAdaptationState,
    )

    state = RegimeAdaptationState(
        as_of=_AS_OF.isoformat().replace("+00:00", "Z"),
        invocation_id=_INVOCATION_ID,
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


def _make_minimal_inputs() -> dict[str, Any]:
    """Return a fixture-tuple dict ready for ``run_decision_pipeline``.

    Uses the empty-portfolio repository fixture (zero positions, $100k cash)
    so the snapshot assembler succeeds end-to-end with no per-position
    pricing complexity.
    """
    from tests.portfolio_state._fixtures import build_minimal_snapshot_inputs

    fixture, quotes, _, config, now = build_minimal_snapshot_inputs()
    return _pipeline_inputs_from_fixture(
        fixture,
        quotes,
        config,
        now,
        active_sectors=("tech", "semis", "financials", "energy"),
    )


def _sector_resolver(ticker: str) -> str:
    return {"NVDA": "tech", "JPM": "financials", "XOM": "energy"}.get(ticker, "tech")


def _make_base_config(prompt: str) -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt=prompt,
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=[],
    )


def _agents_registry() -> dict[AgentName, BaseAgentConfig]:
    """Stringly-keyed agents registry covering every decision-layer slot."""
    return {
        AgentName.analyst: _make_base_config("prompts/decision/analyst.md"),
        AgentName.strategist: _make_base_config("prompts/decision/strategist.md"),
        AgentName.portfolio_manager: _make_base_config("prompts/decision/pm.md"),
    }


# ---------------------------------------------------------------------------
# Result builders for stubbed runners
# ---------------------------------------------------------------------------


def _tokens() -> TokensUsed:
    return TokensUsed(input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0)


def _make_analyst_output(*, mode: Literal["normal", "watchlist"] = "normal") -> Any:
    from alphamind.decision.analyst.models import AnalystOutput

    if mode == "normal":
        return AnalystOutput(
            invocation_id=InvocationId(_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            mode="normal",
            recommendations=(),
            watchlist=None,
        )
    return AnalystOutput(
        invocation_id=InvocationId(_INVOCATION_ID),
        timestamp=_TIMESTAMP,
        mode="watchlist",
        recommendations=None,
        watchlist=(),
    )


def _make_analyst_result(*, mode: Literal["normal", "watchlist"] = "normal") -> AnalystResult:
    return AnalystResult(
        output=_make_analyst_output(mode=mode),
        retry_count=0,
        tokens_used=_tokens(),
        tool_calls_used=0,
        wall_clock_seconds=1.0,
        stop_reason="end_turn",
    )


def _make_strategist_output(*, mode: Literal["normal", "defensive_posture"] = "normal") -> Any:
    from alphamind.decision.strategist.models import (
        DefensivePostureSummary,
        PortfolioLevelObservations,
        StrategistOutput,
    )

    obs = PortfolioLevelObservations(
        aggregate_thesis_health="OK.",
        sector_balance_shifts="OK.",
        thesis_dependency_warnings="OK.",
        capital_allocation_observations="OK.",
        defensive_posture_summary=None
        if mode == "normal"
        else DefensivePostureSummary(
            reduction_priority=(),
            capital_preservation_notes="Trim losers.",
        ),
    )
    return StrategistOutput(
        invocation_id=InvocationId(_INVOCATION_ID),
        timestamp=_TIMESTAMP,
        mode=mode,
        position_assessments=(),
        pending_order_assessments=(),
        portfolio_level_observations=obs,
    )


def _make_strategist_result(
    *, mode: Literal["normal", "defensive_posture"] = "normal"
) -> StrategistResult:
    from alphamind.decision.strategist.validation import ValidationResult

    return StrategistResult(
        output=_make_strategist_output(mode=mode),
        validation_result=ValidationResult(overall="PASS", failures=(), warnings=()),
        tokens_used=_tokens(),
        metadata={"attempts": 1},
    )


def _make_aggregate_observations(
    *, per_rule: tuple[Any, ...] = (), breaches: tuple[Any, ...] = ()
) -> Any:
    from alphamind.decision.proposal_pre_processor.models import (
        AggregateObservations,
        BasisSection,
        BookHealthSummary,
        ByRecommendedAction,
        ByThesisStatus,
        CombinedSetImpact,
        ConvictionDistribution,
        ConvictionHistogram,
    )

    basis = BasisSection(
        analyst_proposal_ids=(),
        strategist_action_ids=(),
        strategist_holds_excluded_count=0,
        snapshot_timestamp=_AS_OF,
    )
    histogram = ConvictionHistogram(
        **{"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
    )
    return AggregateObservations(
        combined_set_impact=CombinedSetImpact(basis=basis, per_rule=per_rule, breaches=breaches),
        conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
        book_health_summary=BookHealthSummary(
            by_thesis_status=ByThesisStatus(
                **{
                    "on-track": 0,
                    "partially-realized": 0,
                    "at-risk": 0,
                    "stale": 0,
                    "invalidated": 0,
                }
            ),
            by_recommended_action=ByRecommendedAction(
                **{"hold": 0, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
            ),
            remedy_flagged_count=0,
            total=0,
        ),
    )


def _make_pre_processor_bundle(
    *,
    aggregate: Any | None = None,
    analyst_mode: Literal["normal", "watchlist"] = "normal",
    strategist_mode: Literal["normal", "defensive_posture"] = "normal",
) -> ProposalPreProcessorBundle:
    from alphamind.decision.proposal_pre_processor.models import (
        AnalystSection,
        StrategistSection,
    )

    return ProposalPreProcessorBundle(
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        aggregate_observations=aggregate or _make_aggregate_observations(),
        strategist_section=StrategistSection(
            mode=strategist_mode,
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=_make_strategist_output(
                mode=strategist_mode
            ).portfolio_level_observations,
        ),
        analyst_section=AnalystSection(
            mode=analyst_mode,
            recommendations=() if analyst_mode == "normal" else None,
            watchlist=() if analyst_mode == "watchlist" else None,
        ),
    )


def _make_pm_result() -> PMResult:
    from alphamind.decision.portfolio_manager.models import (
        PMCompletionRecord,
        VerdictSummary,
    )

    return PMResult(
        output=PMCompletionRecord(
            invocation_id=InvocationId(_INVOCATION_ID),
            timestamp=_TIMESTAMP,
            envelopes_submitted=0,
            verdict_summary=VerdictSummary(
                approve=0,
                approve_with_modification=0,
                reject=0,
            ),
        ),
        submission_log=(),
        retry_count=0,
        tokens_used=_tokens(),
        tool_calls_used=0,
        wall_clock_seconds=1.0,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# CallLog + runner-stub harness
# ---------------------------------------------------------------------------


class _CallLog:
    """Captures the kwargs each stage was invoked with + invocation order."""

    def __init__(self) -> None:
        self.analyst: dict[str, Any] = {}
        self.strategist: dict[str, Any] = {}
        self.pre_processor: dict[str, Any] = {}
        self.pm: dict[str, Any] = {}
        self.order: list[str] = []


@dataclasses.dataclass
class _StageBehavior:
    """Per-stage success value, optional failure to raise, and optional async delay."""

    value: Any
    raises: Exception | None = None
    delay_seconds: float = 0.0


def _make_async_stub(stage_name: str, behavior: _StageBehavior, log: _CallLog) -> Any:
    async def _stub(**kw: Any) -> Any:
        if behavior.delay_seconds:
            await asyncio.sleep(behavior.delay_seconds)
        getattr(log, stage_name).update(kw)
        log.order.append(stage_name)
        if behavior.raises is not None:
            raise behavior.raises
        return behavior.value

    return _stub


def _make_sync_stub(stage_name: str, behavior: _StageBehavior, log: _CallLog) -> Any:
    def _stub(**kw: Any) -> Any:
        getattr(log, stage_name).update(kw)
        log.order.append(stage_name)
        if behavior.raises is not None:
            raise behavior.raises
        return behavior.value

    return _stub


def _patch_runners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log: _CallLog,
    analyst_result: AnalystResult | None = None,
    strategist_result: StrategistResult | None = None,
    pre_processor_bundle: ProposalPreProcessorBundle | None = None,
    pm_result: PMResult | None = None,
    analyst_raises: Exception | None = None,
    strategist_raises: Exception | None = None,
    pre_processor_raises: Exception | None = None,
    pm_raises: Exception | None = None,
    analyst_delay_seconds: float = 0.0,
    strategist_delay_seconds: float = 0.0,
) -> None:
    from alphamind.pipeline import decision as composition

    behaviors = {
        "analyst": _StageBehavior(
            analyst_result or _make_analyst_result(),
            analyst_raises,
            analyst_delay_seconds,
        ),
        "strategist": _StageBehavior(
            strategist_result or _make_strategist_result(),
            strategist_raises,
            strategist_delay_seconds,
        ),
        "pre_processor": _StageBehavior(
            pre_processor_bundle or _make_pre_processor_bundle(),
            pre_processor_raises,
        ),
        "pm": _StageBehavior(pm_result or _make_pm_result(), pm_raises),
    }
    monkeypatch.setattr(
        composition, "run_analyst", _make_async_stub("analyst", behaviors["analyst"], log)
    )
    monkeypatch.setattr(
        composition,
        "run_strategist",
        _make_async_stub("strategist", behaviors["strategist"], log),
    )
    monkeypatch.setattr(
        composition,
        "run_proposal_pre_processor",
        _make_sync_stub("pre_processor", behaviors["pre_processor"], log),
    )
    monkeypatch.setattr(
        composition, "run_portfolio_manager", _make_async_stub("pm", behaviors["pm"], log)
    )


def _drive(**overrides: Any) -> Any:
    from alphamind.pipeline.decision import run_decision_pipeline

    async def _go() -> Any:
        kwargs = _make_minimal_inputs()
        kwargs.update(overrides)
        return await run_decision_pipeline(**kwargs)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Tracer-bullet: happy path normal-mode end-to-end
# ---------------------------------------------------------------------------


def test_pipeline_returns_result_on_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stage succeeds → ``DecisionPipelineResult`` bundles every typed value."""
    from alphamind.pipeline.decision import DecisionPipelineResult
    from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
    from alphamind.risk_guardrails.guardrail_evaluation import (
        PortfolioStateSnapshot as LibrarySnapshot,
    )

    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()

    assert isinstance(result, DecisionPipelineResult)
    assert isinstance(result.pydantic_snapshot, PortfolioStateSnapshot)
    assert isinstance(result.library_snapshot, LibrarySnapshot)
    assert isinstance(result.analyst_result, AnalystResult)
    assert isinstance(result.strategist_result, StrategistResult)
    assert isinstance(result.pre_processor_bundle, ProposalPreProcessorBundle)
    assert isinstance(result.pm_result, PMResult)


def test_pipeline_invokes_stages_in_expected_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """analyst+strategist run before pre-processor, pre-processor before PM."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    # analyst and strategist may swap order under gather; assert relative order.
    assert "analyst" in log.order
    assert "strategist" in log.order
    pre_idx = log.order.index("pre_processor")
    pm_idx = log.order.index("pm")
    a_idx = log.order.index("analyst")
    s_idx = log.order.index("strategist")
    assert max(a_idx, s_idx) < pre_idx < pm_idx


# ---------------------------------------------------------------------------
# Mode dispatch: normal vs halt
# ---------------------------------------------------------------------------


def test_normal_mode_dispatches_normal_to_analyst_and_strategist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``mode='normal'``, both runners receive ``mode='normal'``."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive(mode="normal")
    assert log.analyst["mode"] == "normal"
    assert log.strategist["mode"] == "normal"


def test_halt_mode_dispatches_modal_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``mode='halt'``, analyst gets ``watchlist``, strategist gets
    ``defensive_posture``, PM gets ``halt``."""
    from alphamind.risk_guardrails.breach_behavior import HaltState

    halt_state = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.5,
        daily_drawdown_limit_pct=2.5,
    )
    log = _CallLog()
    _patch_runners(
        monkeypatch,
        log=log,
        analyst_result=_make_analyst_result(mode="watchlist"),
        strategist_result=_make_strategist_result(mode="defensive_posture"),
    )
    _drive(mode="halt", halt_state=halt_state)
    assert log.analyst["mode"] == "watchlist"
    assert log.strategist["mode"] == "defensive_posture"
    assert log.pm["mode"] == "halt"


def test_halt_mode_without_halt_state_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mode='halt'`` with no halt_state raises ValueError before any runner runs."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    with pytest.raises(ValueError, match="halt_state"):
        _drive(mode="halt", halt_state=None)
    # No runner invoked
    assert log.order == []


# ---------------------------------------------------------------------------
# Fail-closed propagation: gather(return_exceptions=False)
# ---------------------------------------------------------------------------


def test_analyst_failure_cancels_strategist_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An analyst-side failure cancels the strategist branch and propagates."""
    failure = SDKFailure(
        "analyst stub failure",
        agent_name=AgentName.analyst.value,
        invocation_id=_INVOCATION_ID,
    )
    log = _CallLog()
    _patch_runners(
        monkeypatch,
        log=log,
        analyst_raises=failure,
        # Use a slow strategist so cancellation can be observed
        strategist_delay_seconds=0.5,
    )
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "pre_processor" not in log.order
    assert "pm" not in log.order


def test_strategist_failure_cancels_analyst_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strategist-side failure cancels the analyst branch and propagates."""
    failure = SDKFailure(
        "strategist stub failure",
        agent_name=AgentName.strategist.value,
        invocation_id=_INVOCATION_ID,
    )
    log = _CallLog()
    _patch_runners(
        monkeypatch,
        log=log,
        strategist_raises=failure,
        analyst_delay_seconds=0.5,
    )
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "pre_processor" not in log.order
    assert "pm" not in log.order


def test_pre_processor_failure_aborts_pm(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the pre-processor raises, the PM does not run."""
    log = _CallLog()
    _patch_runners(
        monkeypatch,
        log=log,
        pre_processor_raises=ValueError("bundle assembly failed"),
    )
    with pytest.raises(ValueError, match="bundle assembly"):
        _drive()
    assert "pm" not in log.order


def test_pm_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PM harness failure propagates from the runner unchanged."""
    failure = SDKFailure(
        "pm stub failure",
        agent_name=AgentName.portfolio_manager.value,
        invocation_id=_INVOCATION_ID,
    )
    log = _CallLog()
    _patch_runners(monkeypatch, log=log, pm_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure


# ---------------------------------------------------------------------------
# Per-trigger override forwarding to PM
# ---------------------------------------------------------------------------


def test_pm_receives_overridden_agent_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``agent_overrides`` carries a portfolio_manager budget knob, the
    forwarded ``agent_config`` reflects the override."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    overrides = {AgentName.portfolio_manager: {"output_token_budget": 9_999}}
    _drive(agent_overrides=overrides)
    forwarded = log.pm["agent_config"]
    assert forwarded.output_token_budget == 9_999


def test_analyst_and_strategist_receive_resolved_agent_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-trigger overrides reach the analyst and strategist via
    ``agent_config`` kwarg."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    overrides = {
        AgentName.analyst: {"latency_budget_seconds": 90},
        AgentName.strategist: {"latency_budget_seconds": 120},
    }
    _drive(agent_overrides=overrides)
    assert log.analyst["agent_config"].latency_budget_seconds == 90
    assert log.strategist["agent_config"].latency_budget_seconds == 120


# ---------------------------------------------------------------------------
# Cross-constraint impact derivation
# ---------------------------------------------------------------------------


def test_cross_constraint_impact_per_rule_count_matches_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CrossConstraintImpact.per_rule`` length equals the bundle's
    ``combined_set_impact.per_rule`` length."""
    from alphamind.decision.proposal_pre_processor.models import PerRuleEntry

    per_rule = (
        PerRuleEntry(
            rule="position_max_size_pct",
            status="PASS",
            current=2.0,
            limit=10.0,
            projected_after=4.0,
            headroom_remaining=6.0,
            unit="% of portfolio",
        ),
        PerRuleEntry(
            rule="net_long_pct",
            status="PASS",
            current=20.0,
            limit=60.0,
            projected_after=22.0,
            headroom_remaining=38.0,
            unit="% of portfolio",
        ),
    )
    bundle = _make_pre_processor_bundle(aggregate=_make_aggregate_observations(per_rule=per_rule))

    log = _CallLog()
    _patch_runners(monkeypatch, log=log, pre_processor_bundle=bundle)
    _drive()
    impact = log.pm["cross_constraint_impact"]
    assert len(impact.per_rule) == 2
    assert {r.rule_id for r in impact.per_rule} == {
        "position_max_size_pct",
        "net_long_pct",
    }


def test_cross_constraint_impact_flagged_rules_match_breaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``flagged_rule_ids`` matches the bundle's ``breaches[].rule`` set."""
    from alphamind.decision.proposal_pre_processor.models import (
        BreachEntry,
        PerRuleEntry,
    )

    per_rule = (
        PerRuleEntry(
            rule="position_max_size_pct",
            status="FAIL",
            current=2.0,
            limit=10.0,
            projected_after=12.0,
            headroom_remaining=-2.0,
            unit="% of portfolio",
        ),
        PerRuleEntry(
            rule="net_long_pct",
            status="PASS",
            current=20.0,
            limit=60.0,
            projected_after=22.0,
            headroom_remaining=38.0,
            unit="% of portfolio",
        ),
    )
    breaches = (
        BreachEntry(
            rule="position_max_size_pct",
            overage=2.0,
            unit="% of portfolio",
            contributors=(),
        ),
    )
    bundle = _make_pre_processor_bundle(
        aggregate=_make_aggregate_observations(per_rule=per_rule, breaches=breaches)
    )

    log = _CallLog()
    _patch_runners(monkeypatch, log=log, pre_processor_bundle=bundle)
    _drive()
    impact = log.pm["cross_constraint_impact"]
    assert set(impact.flagged_rule_ids) == {"position_max_size_pct"}


def test_cross_constraint_impact_capital_sourced_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``available_capital_before_usd`` and ``available_capital_after_usd``
    come from ``cash_ledger.true_deployable_capital_usd``."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()
    impact = log.pm["cross_constraint_impact"]
    deployable = result.pydantic_snapshot.cash_ledger.true_deployable_capital_usd
    assert impact.available_capital_before_usd == deployable
    assert impact.available_capital_after_usd == deployable


# ---------------------------------------------------------------------------
# Pre-processor receives library-shape snapshot
# ---------------------------------------------------------------------------


def test_pre_processor_receives_library_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-processor's ``snapshot`` kwarg is the library-shape snapshot,
    not the Pydantic snapshot."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()
    assert log.pre_processor["snapshot"] is result.library_snapshot


def test_pre_processor_receives_outputs_from_parallel_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-processor receives the analyst and strategist *output* (not
    the Result wrapper)."""
    log = _CallLog()
    a_result = _make_analyst_result()
    s_result = _make_strategist_result()
    _patch_runners(
        monkeypatch,
        log=log,
        analyst_result=a_result,
        strategist_result=s_result,
    )
    _drive()
    assert log.pre_processor["analyst_output"] is a_result.output
    assert log.pre_processor["strategist_output"] is s_result.output


# ---------------------------------------------------------------------------
# Price-lookup wiring (ALP-407 / ALP-449 Slice 2)
# ---------------------------------------------------------------------------


def test_strategist_and_pm_receive_lookups_keyed_to_assembled_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strategist + PM see prices from the pre-built ``AssembledSnapshot``.

    The decision pipeline no longer fetches quotes itself (Slice 2 of
    ALP-449 — assembly is the orchestrator's responsibility); the
    pre-built ``AssembledSnapshot`` carries the materialized ``price_map``
    and the strategist + PM ``current_price_lookup`` callables must
    resolve held tickers via that map.
    """
    from tests.portfolio_state._fixtures import build_multi_position_snapshot_inputs

    log = _CallLog()
    _patch_runners(monkeypatch, log=log)

    async def _go() -> Any:
        from alphamind.pipeline.decision import run_decision_pipeline

        fixture, quotes, _, config, now = build_multi_position_snapshot_inputs()
        inputs = _pipeline_inputs_from_fixture(
            fixture,
            quotes,
            config,
            now,
            active_sectors=("tech", "financials"),
        )
        return await run_decision_pipeline(**inputs)

    asyncio.run(_go())

    # The strategist received a callable price lookup, and resolving each
    # held ticker through it returns the price the fixture seeded.
    strategist_lookup = log.strategist["current_price_lookup"]
    assert strategist_lookup("NVDA") == pytest.approx(510.0)
    assert strategist_lookup("AMD") == pytest.approx(115.0)
    assert strategist_lookup("JPM") == pytest.approx(195.0)
    assert strategist_lookup("AAPL") == pytest.approx(185.0)

    # And the PM received the same lookup — both consumers share the
    # assembler's materialized map.
    pm_lookup = log.pm["current_price_lookup"]
    assert pm_lookup("NVDA") == pytest.approx(510.0)


# ---------------------------------------------------------------------------
# Result dataclass shape
# ---------------------------------------------------------------------------


def test_result_is_frozen_dataclass(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DecisionPipelineResult`` rejects post-construction mutation."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.analyst_result = _make_analyst_result()
