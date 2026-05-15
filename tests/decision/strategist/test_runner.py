"""Tests for the strategist runner — story 07 (ALP-308).

The runner composes :func:`build_initial_validation_state`, the input-bundle
assembler (normal / defensive_posture), and :func:`run_strategist_harness` into
a single ``run_strategist`` entry point. The harness's SDK is stubbed via the
``sdk_query_fn`` dependency-injection parameter — no test calls the real
Anthropic API.

Mirrors the structure of ``tests/decision/analyst/test_runner.py``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml

from alphamind._kernel.money import money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName, AllowedModel, BaseAgentConfig
from alphamind.decision.strategist.harness import HarnessFailure, HarnessSuccess, SDKFailure
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.decision.strategist.runner import (
    StrategistResult,
    load_strategist_agent_config,
    run_strategist,
)
from alphamind.decision.strategist.validation import ValidationResult
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.consumers.strategist import StrategistView
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
from alphamind.risk_guardrails.state_delivery.validation_tool import ValidationToolState

# ---------------------------------------------------------------------------
# Constants and shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
_EXPIRATION_DATE = date(2026, 5, 28)
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30
_PORTFOLIO_VALUE = 1_000_000.0
_AVAILABLE_FOR_NEW_POSITIONS_USD = 200_000.0
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STRATEGIST_PROMPT_TEXT = (_REPO_ROOT / "prompts" / "decision" / "strategist.md").read_text(
    encoding="utf-8"
)


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
        cash_usd=700_000.0,
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
                    underlying="AAPL",
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
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_semis",
                rule_label="Semis sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_financials",
                rule_label="Financials sector concentration",
            ),
            _make_budget_entry(
                rule_id="sector_concentration_energy",
                rule_label="Energy sector concentration",
            ),
            _make_budget_entry(
                rule_id="net_long_pct",
                rule_label="Net long exposure",
            ),
            _make_budget_entry(
                rule_id="gross_exposure_pct",
                rule_label="Gross exposure",
            ),
        )
    )


def _make_param_entry(
    *,
    rule_id: str,
    rule_label: str,
    value: float,
    unit: str = "% of portfolio",
) -> ActiveRiskParameterEntry:
    return ActiveRiskParameterEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        value=value,
        unit=unit,
        regime_multiplier_applied=1.0,
        base_value=value,
    )


def _active_risk_parameters() -> ActiveRiskParameterSet:
    """Risk parameters covering the strategist header renderer's required rules."""
    from tests.decision.conftest import compose_active_risk_parameters_via_orchestrator

    entries = (
        _make_param_entry(
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            value=5.0,
        ),
        _make_param_entry(
            rule_id="daily_drawdown_pct",
            rule_label="Daily drawdown",
            value=2.5,
        ),
        _make_param_entry(
            rule_id="cumulative_drawdown_pct",
            rule_label="Cumulative drawdown",
            value=10.0,
        ),
    )
    return compose_active_risk_parameters_via_orchestrator(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


def _sector_resolver(ticker: str) -> str:
    return {"AAPL": "tech", "NVDA": "tech", "ABC": "tech"}.get(ticker, "tech")


def _state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
    )


def _halt_state() -> HaltState:
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.5,
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


def _strategist_view() -> StrategistView:
    """Empty StrategistView — no positions, no log entries; the runner threads it
    through to the input-bundle assembler unchanged."""
    return StrategistView(
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
    )


def _current_price_lookup(ticker: str) -> float:
    prices = {"AAPL": 175.0, "NVDA": 862.0, "ABC": 100.0}
    return prices[ticker]


def _agent_config(*, latency: int = 30, output_budget: int = 4_000) -> BaseAgentConfig:
    """Minimal BaseAgentConfig pointing to the strategist prompt."""
    return BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/strategist.md",
        latency_budget_seconds=latency,
        context_token_budget=8_000,
        output_token_budget=output_budget,
        tools=["retrieve_brief", "validate_guardrail"],
    )


# ---------------------------------------------------------------------------
# SDK stub helpers
# ---------------------------------------------------------------------------


def _normal_payload(
    *,
    invocation_id: str = "inv-test-001",
    timestamp: str = "2026-05-04T14:30:00+00:00",
) -> dict[str, Any]:
    """Minimal valid normal-mode StrategistOutput payload."""
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "mode": "normal",
        "position_assessments": [],
        "pending_order_assessments": [],
        "portfolio_level_observations": {
            "aggregate_thesis_health": "Empty book; no theses to evaluate.",
            "sector_balance_shifts": "No sector exposure to shift.",
            "thesis_dependency_warnings": "No correlated breakdowns.",
            "capital_allocation_observations": "Cash-only book; capital fully reserved.",
        },
    }


def _defensive_payload(
    *,
    invocation_id: str = "inv-defensive-001",
    timestamp: str = "2026-05-04T14:30:00+00:00",
) -> dict[str, Any]:
    """Minimal valid defensive_posture-mode StrategistOutput payload."""
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "mode": "defensive_posture",
        "position_assessments": [],
        "pending_order_assessments": [],
        "portfolio_level_observations": {
            "aggregate_thesis_health": "All positions in protective mode.",
            "sector_balance_shifts": "No new exposures opened.",
            "thesis_dependency_warnings": "No correlated breakdowns.",
            "capital_allocation_observations": "Capital preservation prioritized.",
            "defensive_posture_summary": {
                "reduction_priority": [],
                "capital_preservation_notes": "Holding cash; no add actions.",
            },
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

    return [
        AssistantMessage(
            content=[TextBlock(text=text)] if text else [],
            model="claude-opus-4-7",
            stop_reason=stop_reason,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        ),
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            stop_reason=stop_reason,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
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
    agent_config: BaseAgentConfig | None = None,
    archive_root: Path | None = None,
    active_sectors: tuple[str, ...] = ("tech", "semis", "financials", "energy"),
) -> dict[str, Any]:
    return {
        "invocation_id": "inv-test-001",
        "timestamp": _AS_OF,
        "mode": mode,
        "halt_state": halt_state,
        "strategist_view": _strategist_view(),
        "synthesizer_brief_text": "## Cross-domain market snapshot\n\nQuiet tape.",
        "retrieval_store": _retrieval_store(),
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": active_sectors,
        "state_delivery_config": _state_delivery_config(),
        "sector_resolver": _sector_resolver,
        "total_portfolio_value_usd": _PORTFOLIO_VALUE,
        "available_for_new_positions_usd": _AVAILABLE_FOR_NEW_POSITIONS_USD,
        "current_price_lookup": _current_price_lookup,
        "profile_feature_flags": _library_config().feature_flags,
        "library_config": _library_config(),
        "library_market": _market_inputs(),
        "starting_snapshot": _portfolio_state_snapshot(),
        "archive_root": archive_root,
        "agent_config": agent_config,
        "sdk_query_fn": sdk_query_fn,
    }


# ---------------------------------------------------------------------------
# 1. load_strategist_agent_config returns the strategist entry from agents.yaml
# ---------------------------------------------------------------------------


def test_load_strategist_agent_config_default_path_returns_strategist_entry() -> None:
    """Default invocation reads the in-tree config/agents.yaml and returns the
    strategist slot's BaseAgentConfig."""
    cfg = load_strategist_agent_config()

    assert isinstance(cfg, BaseAgentConfig)
    assert cfg.prompt == "prompts/decision/strategist.md"


def test_load_strategist_agent_config_override_path(tmp_path: Path) -> None:
    """An override path lets tests/verification scripts read a custom yaml."""
    custom = tmp_path / "agents.yaml"
    base = yaml.safe_load((_REPO_ROOT / "config" / "agents.yaml").read_text())
    base["agents"][AgentName.strategist.value]["output_token_budget"] = 9_999
    custom.write_text(yaml.safe_dump(base))

    cfg = load_strategist_agent_config(custom)

    assert cfg.output_token_budget == 9_999


def test_load_strategist_agent_config_missing_file_raises(tmp_path: Path) -> None:
    """A missing yaml path produces a clear filesystem error."""
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError):
        load_strategist_agent_config(missing)


# ---------------------------------------------------------------------------
# 2. Clean success in normal mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_mode_clean_success_returns_strategist_result(tmp_path: Path) -> None:
    """run_strategist with mode='normal' calls the harness and returns StrategistResult."""
    stub = _make_stub_query([_make_sdk_response(_normal_payload())])

    result = await run_strategist(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=stub,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert isinstance(result, StrategistResult)
    assert isinstance(result.output, StrategistOutput)
    assert result.output.mode == "normal"
    assert isinstance(result.tokens_used, TokensUsed)
    assert isinstance(result.validation_result, ValidationResult)
    assert result.validation_result.overall == "PASS"
    assert result.metadata["mode"] == "normal"
    assert result.metadata["attempts"] == 1


# ---------------------------------------------------------------------------
# 3. Defensive-posture mode requires halt_state; clean success returns defensive output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defensive_posture_mode_clean_success_returns_strategist_result(
    tmp_path: Path,
) -> None:
    """run_strategist with mode='defensive_posture' and halt_state present succeeds."""
    stub = _make_stub_query([_make_sdk_response(_defensive_payload(invocation_id="inv-test-001"))])

    result = await run_strategist(
        **_runner_kwargs(
            mode="defensive_posture",
            sdk_query_fn=stub,
            halt_state=_halt_state(),
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert isinstance(result, StrategistResult)
    assert result.output.mode == "defensive_posture"
    assert result.metadata["mode"] == "defensive_posture"


# ---------------------------------------------------------------------------
# 4. Defensive-posture mode without halt_state raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defensive_posture_mode_without_halt_state_raises(tmp_path: Path) -> None:
    """Passing mode='defensive_posture' but no halt_state raises ValueError before SDK call."""
    stub_called = False

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal stub_called
        stub_called = True
        async for msg in _async_iter(_make_sdk_response(_defensive_payload())):
            yield msg

    with pytest.raises(ValueError, match="halt_state"):
        await run_strategist(
            **_runner_kwargs(
                mode="defensive_posture",
                sdk_query_fn=_stub,
                halt_state=None,
                agent_config=_agent_config(),
                archive_root=tmp_path / "archive",
            )
        )
    assert not stub_called, "SDK must not be called when halt_state guard fires"


# ---------------------------------------------------------------------------
# 5. HarnessFailure propagates unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_failure_propagates_unchanged(tmp_path: Path) -> None:
    """An SDKFailure raised by the harness propagates up unchanged."""
    from claude_agent_sdk import ClaudeSDKError

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise ClaudeSDKError("model API returned 500")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await run_strategist(
            **_runner_kwargs(
                mode="normal",
                sdk_query_fn=_stub,
                agent_config=_agent_config(),
                archive_root=tmp_path / "archive",
            )
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    assert err.invocation_id == "inv-test-001"
    assert err.agent_name == "strategist"


# ---------------------------------------------------------------------------
# 6. agent_config injection bypasses yaml loader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_config_override_propagates_to_harness(tmp_path: Path) -> None:
    """Passing an agent_config bypasses the yaml loader; the override reaches the harness."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_normal_payload())):
            yield msg

    override = _agent_config(latency=99, output_budget=1_234)
    await run_strategist(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_capturing_stub,
            agent_config=override,
            archive_root=tmp_path / "archive",
        )
    )

    options = captured_options[0]
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "1234"


@pytest.mark.asyncio
async def test_default_agent_config_loaded_from_yaml(tmp_path: Path) -> None:
    """When agent_config is omitted, the runner loads the strategist entry from agents.yaml."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_normal_payload())):
            yield msg

    yaml_default = load_strategist_agent_config()
    await run_strategist(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_capturing_stub,
            agent_config=None,
            archive_root=tmp_path / "archive",
        )
    )

    options = captured_options[0]
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(yaml_default.output_token_budget)


# ---------------------------------------------------------------------------
# 7. borrow_cost_resolver propagates to validation state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_borrow_cost_resolver_propagates_to_validation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the caller supplies a ``borrow_cost_resolver``, the runner threads it
    into :func:`build_initial_validation_state`. Mirrors the analyst-runner contract."""
    from alphamind.risk_guardrails.state_delivery import build_initial_validation_state

    captured_kwargs: dict[str, Any] = {}

    def _capturing_builder(**kwargs: Any) -> ValidationToolState:
        captured_kwargs.update(kwargs)
        return build_initial_validation_state(**kwargs)

    monkeypatch.setattr(
        "alphamind.decision.strategist.runner.build_initial_validation_state",
        _capturing_builder,
    )

    sentinel_resolver: Callable[[str], float] = lambda ticker: 0.0125  # noqa: E731
    await run_strategist(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_make_stub_query([_make_sdk_response(_normal_payload())]),
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        ),
        borrow_cost_resolver=sentinel_resolver,
    )

    assert captured_kwargs["borrow_cost_resolver"] is sentinel_resolver


# ---------------------------------------------------------------------------
# 8. System prompt is read from agent_config.prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_loaded_from_agent_config(tmp_path: Path) -> None:
    """The runner reads the prompt path from agent_config.prompt and sends its
    file contents to the harness as the system prompt."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_normal_payload())):
            yield msg

    await run_strategist(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_capturing_stub,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    # The harness places the agent_config.prompt file contents on options.system_prompt.
    assert captured_options[0].system_prompt == _STRATEGIST_PROMPT_TEXT


# ---------------------------------------------------------------------------
# Module-imports sanity check
# ---------------------------------------------------------------------------


def test_module_lifecycle_imports() -> None:
    """A static guard that the runner re-exports the surface AC1 mandates."""
    from alphamind.decision.strategist import runner as runner_module

    assert hasattr(runner_module, "run_strategist")
    assert hasattr(runner_module, "StrategistResult")
    assert hasattr(runner_module, "load_strategist_agent_config")


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants (ALP-475: 10b conversion)
# ---------------------------------------------------------------------------


def _zero_tokens() -> TokensUsed:
    return TokensUsed(
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


class TestStrategistResultIsFrozenDataclass:
    def test_strategist_result_is_frozen_dataclass(self) -> None:
        output = StrategistOutput.model_validate(_normal_payload())
        result = StrategistResult(
            output=output,
            validation_result=ValidationResult(overall="PASS", failures=(), warnings=()),
            tokens_used=_zero_tokens(),
            metadata={},
        )
        assert dataclasses.is_dataclass(StrategistResult)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.tokens_used = _zero_tokens()  # type: ignore[misc]

    def test_harness_success_is_frozen_dataclass(self) -> None:
        output = StrategistOutput.model_validate(_normal_payload())
        success = HarnessSuccess(
            output=output,
            validation_result=ValidationResult(overall="PASS", failures=(), warnings=()),
            tokens_used=_zero_tokens(),
            metadata={"attempts": 1},
        )
        assert dataclasses.is_dataclass(HarnessSuccess)
        with pytest.raises(dataclasses.FrozenInstanceError):
            success.tokens_used = _zero_tokens()  # type: ignore[misc]
