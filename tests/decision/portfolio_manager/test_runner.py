"""Tests for the portfolio-manager runner — story 08 (ALP-330).

The runner composes :func:`build_initial_validation_state`,
:func:`build_initial_submit_envelope_state`, the input-bundle assembler
(normal / halt), and :func:`invoke_pm` into a single ``run_portfolio_manager``
entry point. The harness's SDK is stubbed via the ``sdk_query_fn`` dependency-
injection parameter — no test calls the real Anthropic API.

Mirrors the structure of ``tests/decision/strategist/test_runner.py``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import Price, money, price
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.decision.portfolio_manager.harness import (
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
)
from alphamind.decision.portfolio_manager.models import PMCompletionRecord
from alphamind.decision.portfolio_manager.runner import (
    PM_TOOL_NAMES,
    PMResult,
    load_pm_agent_config,
    run_portfolio_manager,
)
from alphamind.decision.proposal_pre_processor.models import (
    AggregateObservations,
    AnalystSection,
    BasisSection,
    BookHealthSummary,
    ByRecommendedAction,
    ByThesisStatus,
    CombinedSetImpact,
    ConvictionDistribution,
    ConvictionHistogram,
    ProposalPreProcessorBundle,
    StrategistSection,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.consumers.portfolio_manager import (
    PortfolioManagerThesisComponentReader,
    PortfolioManagerView,
)
from alphamind.portfolio_state.records.theses import ThesisComponent
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
)
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    EscalationZones,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    CrossConstraintImpact,
)

# ---------------------------------------------------------------------------
# Constants and shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
_NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_EXPIRATION_DATE = date(2026, 5, 28)
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30
_PORTFOLIO_VALUE = 100_000.0
_AVAILABLE_FOR_NEW_POSITIONS_USD = 20_000.0
_DEFAULT_ACTIVE_SECTORS = frozenset({"tech", "semis", "financials", "energy"})
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _library_config() -> LibraryConfig:
    effective_limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 40.0,
        "gross_exposure_pct": 100.0,
        "options_delta_pct": 30.0,
        "portfolio_theta_pct_per_day": 0.5,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _portfolio_state_snapshot(
    *,
    sector_exposure_pct: Mapping[str, float] | None = None,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {"tech": 18.3, "semis": 6.0, "financials": 3.0, "energy": 3.0}
    return PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct)),
        net_long_pct=30.0,
        net_short_pct=0.0,
        gross_pct=78.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )


def _market_inputs(underlyings: Sequence[str] = ("AAPL", "NVDA", "ABC")) -> MarketInputs:
    return MarketInputs(
        underlying_prices=MappingProxyType({u: _SPOT for u in underlyings}),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=FixtureIvProvider(
            surface={
                "AAPL": IvSurfaceEntry(
                    underlying=Symbol("AAPL"),
                    quotes=(
                        IvQuote(
                            strike=100.0,
                            expiration=_EXPIRATION_DATE,
                            contract_type=ContractType.CALL,
                            implied_volatility=_IV,
                        ),
                        IvQuote(
                            strike=100.0,
                            expiration=_EXPIRATION_DATE,
                            contract_type=ContractType.PUT,
                            implied_volatility=_IV,
                        ),
                    ),
                ),
            },
            realized_vol={},
        ),
        as_of=_AS_OF,
    )


def _make_budget_entry(*, rule_id: str, rule_label: str) -> RiskBudgetEntry:
    """Build a budget entry with normal-zone headroom under the limit."""
    current_value = 5.0
    limit_value = 25.0
    headroom = limit_value - current_value
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=(headroom / limit_value) * 100.0,
        zone=RiskZone.NORMAL,
        unit="% of portfolio",
        cumulative_invocation_impact_value=0.0,
    )


def _risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(
        entries=(
            _make_budget_entry(
                rule_id="sector_concentration_tech", rule_label="Tech sector concentration"
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis", rule_label="Semis sector concentration"
            ),
            _make_budget_entry(
                rule_id="sector_concentration_financials",
                rule_label="Financials sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_energy", rule_label="Energy sector concentration"
            ),
            _make_budget_entry(rule_id="net_long_pct", rule_label="Net long exposure"),
            _make_budget_entry(rule_id="gross_exposure_pct", rule_label="Gross exposure"),
            _make_budget_entry(rule_id="daily_drawdown_pct", rule_label="Daily drawdown"),
        )
    )


def _make_param_entry(*, rule_id: str, rule_label: str, value: float) -> ActiveRiskParameterEntry:
    return ActiveRiskParameterEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        value=value,
        unit="% of portfolio",
        regime_multiplier_applied=1.0,
        base_value=value,
    )


def _active_risk_parameters() -> ActiveRiskParameterSet:
    """Risk parameters covering the PM header renderer's required rules."""
    from tests.decision.conftest import compose_active_risk_parameters_via_orchestrator

    return compose_active_risk_parameters_via_orchestrator(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            _make_param_entry(
                rule_id="position_max_size_pct", rule_label="Per-position max size", value=5.0
            ),
            _make_param_entry(rule_id="daily_drawdown_pct", rule_label="Daily drawdown", value=2.5),
            _make_param_entry(
                rule_id="cumulative_drawdown_pct",
                rule_label="Cumulative drawdown",
                value=10.0,
            ),
        ),
        active_overlays=(),
    )


def _sector_resolver(ticker: str) -> str:
    return {"AAPL": "tech", "NVDA": "semis", "ABC": "tech"}.get(ticker, "tech")


def _state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=4,
        abandoned_window_lookback_invocations=1,
    )


def _halt_state() -> HaltState:
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.6,
        daily_drawdown_limit_pct=2.5,
    )


def _retrieval_store() -> RetrievalStore:
    return RetrievalStore(entries={}, freshness_by_source={})


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=money(0.0),
        total_unrealized_pnl_pct_of_portfolio=0.0,
        daily_realized_pnl_usd=money(0.0),
        daily_total_pnl_usd=money(0.0),
        cumulative_realized_pnl_usd=money(0.0),
        rolling_realized_pnl={
            "1d": money(0.0),
            "3d": money(0.0),
            "5d": money(0.0),
            "20d": money(0.0),
        },
        win_rate_pct=0.0,
        average_win_size_usd=money(0.0),
        average_loss_size_usd=money(0.0),
        profit_factor=0.0,
    )


def _make_drawdown() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=_PORTFOLIO_VALUE,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=money(0.0),
        total_short_delta_adjusted_usd=money(0.0),
        net_directional_pct_of_portfolio=0.0,
        gross_pct_of_portfolio=0.0,
    )


def _make_thesis_quality_aggregates() -> ThesisQualityAggregate:
    return ThesisQualityAggregate(
        as_of_timestamp=_AS_OF,
        resolution_counts_by_window=(),
        duration_stats_by_window=(),
        invalidation_timing_stats_by_window=(),
        signal_hit_rates=(),
        signal_to_thesis_conversions=(),
        conviction_calibration=(),
        conviction_sizing_deviation_by_window=(),
        performance_attribution=(),
        alpha_beta_decomposition_by_window=(),
    )


def _pm_view() -> PortfolioManagerView:
    """Minimal PortfolioManagerView for runner tests — no positions, full
    aggregates."""
    return PortfolioManagerView(
        positions=(),
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        risk_budget=_risk_budget(),
        active_risk_parameters=_active_risk_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=_make_thesis_quality_aggregates(),
        position_modification_trail={},
    )


def _thesis_component_reader() -> PortfolioManagerThesisComponentReader:
    class _StubReader:
        async def get_thesis_components(self, position_id: str) -> tuple[ThesisComponent, ...]:
            return ()

    return _StubReader()


def _pre_processor_bundle() -> ProposalPreProcessorBundle:
    """Minimal pre-processor bundle for runner tests — no proposals."""
    histogram = ConvictionHistogram.model_validate({"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
    by_thesis = ByThesisStatus.model_validate(
        {"on-track": 0, "partially-realized": 0, "at-risk": 0, "stale": 0, "invalidated": 0}
    )
    by_action = ByRecommendedAction.model_validate(
        {"hold": 0, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
    )
    aggregate = AggregateObservations(
        combined_set_impact=CombinedSetImpact(
            basis=BasisSection(
                analyst_proposal_ids=(),
                strategist_action_ids=(),
                strategist_holds_excluded_count=0,
                snapshot_timestamp=_NOW,
            ),
            per_rule=(),
            breaches=(),
        ),
        conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
        book_health_summary=BookHealthSummary(
            by_thesis_status=by_thesis,
            by_recommended_action=by_action,
            remedy_flagged_count=0,
            total=0,
        ),
    )
    return ProposalPreProcessorBundle.model_construct(
        invocation_id="inv-pm-001",
        timestamp=_NOW,
        aggregate_observations=aggregate,
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=None,
        ),
        analyst_section=AnalystSection.model_construct(
            mode="normal", recommendations=(), watchlist=None
        ),
    )


def _cross_constraint_impact() -> CrossConstraintImpact:
    return CrossConstraintImpact(
        per_rule=(),
        flagged_rule_ids=(),
        available_capital_before_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
        available_capital_after_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
    )


def _current_price_lookup(ticker: str) -> Price:
    prices = {"AAPL": "175.0", "NVDA": "862.0", "ABC": "100.0"}
    return price(prices[ticker])


def _agent_config(*, latency: int = 30, output_budget: int = 4_000) -> BaseAgentConfig:
    """Minimal BaseAgentConfig pointing to the PM prompt."""
    return BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/pm.md",
        latency_budget_seconds=latency,
        context_token_budget=8_000,
        output_token_budget=output_budget,
        tools=["retrieve_brief", "validate_guardrail", "get_thesis_components", "submit_envelope"],
    )


# ---------------------------------------------------------------------------
# SDK stub helpers
# ---------------------------------------------------------------------------


def _completion_payload(
    *,
    invocation_id: str = "inv-pm-001",
    timestamp: str = "2026-05-04T14:30:00+00:00",
    envelopes_submitted: int = 0,
    approve: int = 0,
    approve_with_modification: int = 0,
    reject: int = 0,
    override_with_corrective_action: int = 0,
) -> dict[str, Any]:
    """Minimal valid PMCompletionRecord payload (no-op invocation by default)."""
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "envelopes_submitted": envelopes_submitted,
        "verdict_summary": {
            "approve": approve,
            "approve_with_modification": approve_with_modification,
            "reject": reject,
            "override_with_corrective_action": override_with_corrective_action,
        },
    }


def _make_sdk_response(
    structured_output: dict[str, Any] | None,
    *,
    text: str = "",
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    return [
        AssistantMessage(
            content=[TextBlock(text=text)] if text else [],
            model="claude-opus-4-7",
            stop_reason=stop_reason,
            usage=usage,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            stop_reason=stop_reason,
            usage=usage,
            structured_output=structured_output,
        ),
    ]


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _make_stub_query(responses: list[list[Any]]) -> Callable[..., AsyncGenerator[Any]]:
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        async for msg in _async_iter(responses[idx]):
            yield msg

    return _stub


def _runner_kwargs(
    *,
    mode: str = "normal",
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    halt_state: HaltState | None = None,
    pending_orders: tuple[Any, ...] = (),
    current_price_lookup: Callable[[str], Price] | None = None,
    agent_config: BaseAgentConfig | None = None,
    archive_root: Path | None = None,
    active_sectors: frozenset[str] = _DEFAULT_ACTIVE_SECTORS,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "pre_processor_bundle": _pre_processor_bundle(),
        "synthesizer_text": "## Cross-domain market snapshot\n\nQuiet tape.",
        "retrieval_store": _retrieval_store(),
        "pm_view": _pm_view(),
        "thesis_component_reader": _thesis_component_reader(),
        "risk_budget": _risk_budget(),
        "active_risk_parameters": _active_risk_parameters(),
        "profile_feature_flags": _library_config().feature_flags,
        "library_config": _library_config(),
        "library_market": _market_inputs(),
        "sector_resolver": _sector_resolver,
        "portfolio_state_snapshot": _portfolio_state_snapshot(),
        "active_sectors": active_sectors,
        "invocation_id": "inv-pm-001",
        "timestamp": _AS_OF,
        "state_delivery_config": _state_delivery_config(),
        "options_enabled": False,
        "short_selling_enabled": False,
        "total_portfolio_value_usd": _PORTFOLIO_VALUE,
        "available_for_new_positions_usd": _AVAILABLE_FOR_NEW_POSITIONS_USD,
        "cross_constraint_impact": _cross_constraint_impact(),
        "halt_state": halt_state,
        "pending_orders": pending_orders,
        "current_price_lookup": current_price_lookup,
        "archive_root": archive_root,
        "agent_config": agent_config,
        "sdk_query_fn": sdk_query_fn,
    }


# ---------------------------------------------------------------------------
# 1. load_pm_agent_config returns the portfolio_manager entry from agents.yaml
# ---------------------------------------------------------------------------


def test_runner_loads_agent_config() -> None:
    """Default invocation reads in-tree config/agents.yaml and returns the PM slot."""
    cfg = load_pm_agent_config()

    assert isinstance(cfg, BaseAgentConfig)
    assert cfg.prompt == "prompts/decision/pm.md"


# ---------------------------------------------------------------------------
# 2. Normal-mode dispatch routes through assemble_input_bundle_normal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_dispatches_normal_mode_to_normal_assembler(tmp_path: Path) -> None:
    """Given mode='normal', the assembled user message contains the normal-mode
    ``=== GUARDRAIL STATE ===`` block and no halt banner."""
    captured_prompts: list[str] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        async for msg in _async_iter(_make_sdk_response(_completion_payload())):
            yield msg

    await run_portfolio_manager(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_capturing_stub,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert len(captured_prompts) == 1
    user_message = captured_prompts[0]
    assert "=== GUARDRAIL STATE" in user_message
    assert "HALT MODE ACTIVE" not in user_message


# ---------------------------------------------------------------------------
# 3. Halt-mode dispatch routes through assemble_input_bundle_halt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_dispatches_halt_mode_to_halt_assembler(tmp_path: Path) -> None:
    """Given mode='halt' + halt_state, the user message contains the halt banner."""
    captured_prompts: list[str] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        async for msg in _async_iter(_make_sdk_response(_completion_payload())):
            yield msg

    await run_portfolio_manager(
        **_runner_kwargs(
            mode="halt",
            sdk_query_fn=_capturing_stub,
            halt_state=_halt_state(),
            current_price_lookup=_current_price_lookup,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert len(captured_prompts) == 1
    user_message = captured_prompts[0]
    assert "HALT MODE ACTIVE" in user_message


# ---------------------------------------------------------------------------
# 4. Halt mode without halt_state raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_raises_on_halt_mode_without_halt_state(tmp_path: Path) -> None:
    """Passing mode='halt' but halt_state=None raises ValueError before SDK call."""
    stub_called = False

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal stub_called
        stub_called = True
        async for msg in _async_iter(_make_sdk_response(_completion_payload())):
            yield msg

    with pytest.raises(ValueError, match="halt_state"):
        await run_portfolio_manager(
            **_runner_kwargs(
                mode="halt",
                sdk_query_fn=_stub,
                halt_state=None,
                current_price_lookup=_current_price_lookup,
                agent_config=_agent_config(),
                archive_root=tmp_path / "archive",
            )
        )

    assert not stub_called, "SDK must not be called when halt_state guard fires"


# ---------------------------------------------------------------------------
# 5. Halt mode without current_price_lookup raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_raises_on_halt_mode_without_current_price_lookup(tmp_path: Path) -> None:
    """Passing mode='halt' + halt_state but no current_price_lookup raises ValueError."""
    stub_called = False

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal stub_called
        stub_called = True
        async for msg in _async_iter(_make_sdk_response(_completion_payload())):
            yield msg

    with pytest.raises(ValueError, match="current_price_lookup"):
        await run_portfolio_manager(
            **_runner_kwargs(
                mode="halt",
                sdk_query_fn=_stub,
                halt_state=_halt_state(),
                current_price_lookup=None,
                agent_config=_agent_config(),
                archive_root=tmp_path / "archive",
            )
        )

    assert not stub_called, "SDK must not be called when current_price_lookup guard fires"


# ---------------------------------------------------------------------------
# 6. Each invocation constructs fresh state cells
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_constructs_state_cells_per_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two consecutive runner invocations build distinct state-cell instances —
    the validation and submit_envelope cells are never reused across invocations.
    """
    captured_validation: list[Any] = []
    captured_submit_envelope: list[Any] = []

    from alphamind.decision.portfolio_manager import harness as harness_module
    from alphamind.decision.portfolio_manager import runner as runner_module

    real_invoke_pm = harness_module.invoke_pm

    async def _capturing_invoke_pm(**kwargs: Any) -> Any:
        captured_validation.append(kwargs["initial_validation_state"])
        captured_submit_envelope.append(kwargs["initial_submit_envelope_state"])
        # The subprocess wrapper passes ``broker_dispatch`` to ``invoke_pm`` but
        # the wrapper raises ``NotImplementedError`` on non-None broker_dispatch
        # before reaching the in-process branch; force-None here so this
        # capturing fake mirrors the in-process call shape exactly.
        kwargs.setdefault("broker_dispatch", None)
        return await real_invoke_pm(**kwargs)

    # Post-ALP-650 the runner calls ``invoke_portfolio_manager_in_subprocess``;
    # patch the wrapper symbol on the runner module so this fake intercepts the
    # call. The ``sdk_query_fn=stub`` argument routes the wrapper to its
    # in-process branch (so the live SDK is never spawned), but the wrapper
    # itself must still be patched to capture the state-cells.
    monkeypatch.setattr(
        runner_module, "invoke_portfolio_manager_in_subprocess", _capturing_invoke_pm
    )

    stub = _make_stub_query(
        [_make_sdk_response(_completion_payload()), _make_sdk_response(_completion_payload())]
    )

    await run_portfolio_manager(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=stub,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )
    await run_portfolio_manager(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=stub,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert len(captured_validation) == 2
    assert len(captured_submit_envelope) == 2
    assert captured_validation[0] is not captured_validation[1]
    assert captured_submit_envelope[0] is not captured_submit_envelope[1]
    # The submit-envelope cell wraps the SAME validation-state cell so
    # cumulative-impact tracking is unified across pre-submission validation
    # and submit-time re-validation (parent decision (I)).
    assert captured_submit_envelope[0].validation_state is captured_validation[0]
    assert captured_submit_envelope[1].validation_state is captured_validation[1]


# ---------------------------------------------------------------------------
# 7. HarnessFailure propagates unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_propagates_harness_failure(tmp_path: Path) -> None:
    """A MalformedOutputFailure raised by the harness propagates up unchanged."""
    bad_payload = {"shape": "wrong"}
    stub = _make_stub_query([_make_sdk_response(bad_payload), _make_sdk_response(bad_payload)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await run_portfolio_manager(
            **_runner_kwargs(
                mode="normal",
                sdk_query_fn=stub,
                agent_config=_agent_config(),
                archive_root=tmp_path / "archive",
            )
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    assert err.invocation_id == "inv-pm-001"
    assert err.agent_name == "portfolio_manager"


# ---------------------------------------------------------------------------
# 8. PMResult exposes the engine-stub's submission log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_returns_pmresult_with_submission_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the harness's submission log is non-empty, PMResult exposes it
    through the ``submission_log`` field. The runner does not synthesize the
    log itself — it forwards the harness's tuple."""
    from alphamind.analysis._shared import TokensUsed
    from alphamind.decision.portfolio_manager import models as pm_models
    from alphamind.decision.portfolio_manager import runner as runner_module
    from alphamind.decision.portfolio_manager.harness import HarnessSuccess
    from alphamind.decision.portfolio_manager.submit_envelope import (
        Acknowledgment,
        SubmissionLogEntry,
        SubmissionResult,
    )

    sentinel_envelope = pm_models.PMAnalystEnvelope.model_construct(
        envelope_id="ENV-REC-1",
        source_provenance="pm_analyst",
        source_recommendation_id=1,
        verdict="approve",
        rationale_narrative="approved",
        evaluation=pm_models.ThesisQualityEvaluation.model_construct(),
        position_id=None,
        commands=(),
        modifications=(),
        concerns=(),
        anti_patterns_identified=(),
    )
    sentinel_log = (
        SubmissionLogEntry(
            envelope=sentinel_envelope,
            submission_results=(
                SubmissionResult(
                    command_ordinal=0,
                    status="accepted",
                    command_id="inv-pm-001.ENV-REC-1.0.0",
                    acknowledgment=Acknowledgment(),
                ),
            ),
        ),
    )

    completion_record = PMCompletionRecord.model_validate(_completion_payload())

    async def _fake_invoke_pm(**kwargs: Any) -> HarnessSuccess:
        return HarnessSuccess(
            output=completion_record,
            retry_count=0,
            tokens_used=TokensUsed(
                input_tokens=10, output_tokens=20, cache_read_tokens=0, cache_write_tokens=0
            ),
            tool_calls_used=1,
            wall_clock_seconds=0.5,
            stop_reason="end_turn",
            submission_log=sentinel_log,
        )

    # Post-ALP-650 the runner calls ``invoke_portfolio_manager_in_subprocess``;
    # the patch targets that symbol on the runner module so this fake replaces
    # the wrapper entirely (no subprocess spawn, no pickle attempt).
    monkeypatch.setattr(runner_module, "invoke_portfolio_manager_in_subprocess", _fake_invoke_pm)

    result = await run_portfolio_manager(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=None,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert isinstance(result, PMResult)
    assert isinstance(result.output, PMCompletionRecord)
    assert result.submission_log == sentinel_log
    assert len(result.submission_log) == 1
    assert result.submission_log[0].envelope.envelope_id == "ENV-REC-1"


# ---------------------------------------------------------------------------
# Module-imports sanity check — verifies the AC #1 / AC #2 public surface
# ---------------------------------------------------------------------------


def test_module_lifecycle_imports() -> None:
    """The runner module re-exports the surface AC1 mandates, and PM_TOOL_NAMES
    enumerates the canonical wire-form MCP tool names the harness wires —
    including both single-call and batch validators (ALP-621 Finding 7)."""
    from alphamind.decision.portfolio_manager import runner as runner_module

    assert hasattr(runner_module, "run_portfolio_manager")
    assert hasattr(runner_module, "PMResult")
    assert hasattr(runner_module, "load_pm_agent_config")
    assert hasattr(runner_module, "PM_TOOL_NAMES")

    # The canonical wire-form MCP tool names — the PM's tool surface.
    assert PM_TOOL_NAMES == (
        "mcp__alphamind_decision_validation__validate_guardrail",
        "mcp__alphamind_decision_validation__validate_guardrail_batch",
        "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
        "mcp__alphamind_portfolio_state_thesis_components__get_thesis_components",
        "mcp__alphamind_execution_oms_submit__submit_envelope",
    )

    # The package re-exports the runner surface.
    from alphamind.decision import portfolio_manager as pm_pkg

    assert pm_pkg.run_portfolio_manager is runner_module.run_portfolio_manager
    assert pm_pkg.PMResult is runner_module.PMResult
    assert pm_pkg.load_pm_agent_config is runner_module.load_pm_agent_config
    assert pm_pkg.PM_TOOL_NAMES is runner_module.PM_TOOL_NAMES


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants (ALP-475: 10b conversion)
# ---------------------------------------------------------------------------


class TestPMResultIsFrozenDataclass:
    def test_pm_result_is_frozen_dataclass(self) -> None:
        from alphamind.analysis._shared import TokensUsed

        completion_record = PMCompletionRecord.model_validate(_completion_payload())
        result = PMResult(
            output=completion_record,
            submission_log=(),
            retry_count=0,
            tokens_used=TokensUsed(
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            tool_calls_used=0,
            wall_clock_seconds=1.0,
            stop_reason="end_turn",
        )
        assert dataclasses.is_dataclass(PMResult)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.retry_count = 999  # type: ignore[misc]

    def test_harness_success_is_frozen_dataclass(self) -> None:
        from alphamind.analysis._shared import TokensUsed

        completion_record = PMCompletionRecord.model_validate(_completion_payload())
        success = HarnessSuccess(
            output=completion_record,
            retry_count=0,
            tokens_used=TokensUsed(
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            tool_calls_used=0,
            wall_clock_seconds=1.0,
            stop_reason="end_turn",
            submission_log=(),
        )
        assert dataclasses.is_dataclass(HarnessSuccess)
        with pytest.raises(dataclasses.FrozenInstanceError):
            success.retry_count = 999  # type: ignore[misc]
