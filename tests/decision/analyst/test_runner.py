"""Tests for the analyst runner — story 08 (ALP-299).

The runner composes :func:`build_initial_validation_state`, the input-bundle
assembler (normal / halt), and :func:`invoke_analyst` into a single
``run_analyst`` entry point. The harness's SDK is stubbed via the
``sdk_query_fn`` dependency-injection parameter — no test calls the real
Anthropic API.
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

from alphamind._kernel.ids import InvocationId, Symbol
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AgentName, AllowedModel, BaseAgentConfig
from alphamind.decision.analyst.harness import HarnessFailure, HarnessSuccess, SDKFailure
from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.analyst.runner import (
    AnalystResult,
    load_analyst_agent_config,
    run_analyst,
)
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAvailableCapital,
    AnalystView,
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
# Shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
_EXPIRATION_DATE = date(2026, 5, 28)
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30
_PORTFOLIO_VALUE = 100_000.0


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
    """Risk-budget covering the four-way active-sector taxonomy plus the
    net-long / gross-exposure entries the renderer requires.

    The state-delivery renderer requires one ``sector_concentration_<sector>``
    entry per active sector, plus ``net_long_pct`` and ``gross_exposure_pct``,
    or it raises.
    """
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


def _active_risk_parameters() -> ActiveRiskParameterSet:
    from tests.decision.conftest import compose_active_risk_parameters_via_orchestrator

    return compose_active_risk_parameters_via_orchestrator(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="per_position_max_size",
                rule_label="Per-position max size",
                value=5.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
        ),
        active_overlays=(),
    )


def _sector_resolver(ticker: str) -> str:
    return {"AAPL": "tech", "NVDA": "tech", "ABC": "tech"}.get(ticker, "tech")


def _analyst_view() -> AnalystView:
    return AnalystView(
        held_positions=(),
        active_thesis_summaries=(),
        available_capital=AnalystAvailableCapital(
            available_for_new_positions_usd=70_000.0,
            available_for_new_positions_pct=70.0,
            per_position_max_size_usd=5_000.0,
            per_position_max_size_pct=5.0,
        ),
        pending_orders=(),
        abandoned_openings=(),
    )


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


def _agent_config(*, latency: int = 30, output_budget: int = 4_000) -> BaseAgentConfig:
    """Minimal BaseAgentConfig pointing to the analyst prompt."""
    return BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/analyst.md",
        latency_budget_seconds=latency,
        context_token_budget=8_000,
        output_token_budget=output_budget,
        tools=["retrieve_brief", "validate_guardrail"],
    )


# ---------------------------------------------------------------------------
# SDK stub helpers
# ---------------------------------------------------------------------------


def _normal_mode_payload(
    *,
    invocation_id: str = "inv-test-001",
    timestamp: str = "2026-05-04T14:30:00+00:00",
) -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "mode": "normal",
        "recommendations": [],
    }


def _watchlist_payload(
    *,
    invocation_id: str = "inv-test-001",
    timestamp: str = "2026-05-04T14:30:00+00:00",
    sector: str = "tech",
) -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "mode": "watchlist",
        "watchlist": [
            {
                "ticker": "NVDA",
                "sector": sector,
                "thesis_summary": "Sentiment turning constructive.",
                "estimated_conviction": 3,
            }
        ],
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
    active_sectors: frozenset[str] | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "synthesizer_text": "Synthesizer brief text.",
        "retrieval_store": _retrieval_store(),
        "analyst_view": _analyst_view(),
        "risk_budget": _risk_budget(),
        "active_risk_parameters": _active_risk_parameters(),
        "profile_feature_flags": _library_config().feature_flags,
        "library_config": _library_config(),
        "library_market": _market_inputs(),
        "sector_resolver": _sector_resolver,
        "portfolio_state_snapshot": _portfolio_state_snapshot(),
        "active_sectors": active_sectors
        if active_sectors is not None
        else frozenset({"tech", "semis", "financials", "energy"}),
        "invocation_id": "inv-test-001",
        "timestamp": _AS_OF,
        "state_delivery_config": _state_delivery_config(),
        "options_enabled": False,
        "short_selling_enabled": False,
        "halt_state": halt_state,
        "archive_root": archive_root,
        "sdk_query_fn": sdk_query_fn,
        "agent_config": agent_config,
    }


# ---------------------------------------------------------------------------
# 1. load_analyst_agent_config returns the analyst entry from agents.yaml
# ---------------------------------------------------------------------------


def test_load_analyst_agent_config_default_path_returns_analyst_entry() -> None:
    """Default invocation reads the in-tree config/agents.yaml and returns the
    analyst slot's ``BaseAgentConfig``."""
    cfg = load_analyst_agent_config()

    assert isinstance(cfg, BaseAgentConfig)
    assert cfg.model == AllowedModel.opus_4_7
    assert cfg.prompt == "prompts/decision/analyst.md"


def test_load_analyst_agent_config_override_path(tmp_path: Path) -> None:
    """An override path lets tests/verification scripts read a custom yaml."""
    custom = tmp_path / "agents.yaml"
    base = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "config" / "agents.yaml").read_text()
    )
    # Bump the analyst's output budget so we can detect the override took effect.
    base["agents"][AgentName.analyst.value]["output_token_budget"] = 9_999
    custom.write_text(yaml.safe_dump(base))

    cfg = load_analyst_agent_config(custom)

    assert cfg.output_token_budget == 9_999


def test_load_analyst_agent_config_missing_file_raises(tmp_path: Path) -> None:
    """A missing yaml path produces a clear filesystem error rather than a silent default."""
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError):
        load_analyst_agent_config(missing)


# ---------------------------------------------------------------------------
# 2. Clean success in normal mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_mode_clean_success_returns_analyst_result(
    tmp_path: Path,
) -> None:
    """run_analyst with mode='normal' calls the harness and returns AnalystResult."""
    stub = _make_stub_query([_make_sdk_response(_normal_mode_payload())])

    result = await run_analyst(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=stub,
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert isinstance(result, AnalystResult)
    assert isinstance(result.output, AnalystOutput)
    assert result.output.mode == "normal"
    assert result.retry_count == 0
    assert isinstance(result.tokens_used, TokensUsed)
    assert result.tool_calls_used == 0
    assert result.wall_clock_seconds >= 0.0
    assert result.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# 3. Watchlist mode requires halt_state; clean success returns watchlist output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_mode_clean_success_returns_analyst_result(
    tmp_path: Path,
) -> None:
    """run_analyst with mode='watchlist' and halt_state present returns watchlist output."""
    stub = _make_stub_query([_make_sdk_response(_watchlist_payload(sector="tech"))])

    result = await run_analyst(
        **_runner_kwargs(
            mode="watchlist",
            sdk_query_fn=stub,
            halt_state=_halt_state(),
            agent_config=_agent_config(),
            archive_root=tmp_path / "archive",
        )
    )

    assert isinstance(result, AnalystResult)
    assert result.output.mode == "watchlist"
    assert result.output.watchlist is not None
    assert len(result.output.watchlist) == 1


# ---------------------------------------------------------------------------
# 4. Watchlist mode without halt_state raises a clear error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_mode_without_halt_state_raises(
    tmp_path: Path,
) -> None:
    """Passing mode='watchlist' but no halt_state raises ValueError before SDK call."""
    stub_called = False

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal stub_called
        stub_called = True
        async for msg in _async_iter(_make_sdk_response(_watchlist_payload())):
            yield msg

    with pytest.raises(ValueError, match="halt_state"):
        await run_analyst(
            **_runner_kwargs(
                mode="watchlist",
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
        await run_analyst(
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
    assert err.agent_name == "analyst"


# ---------------------------------------------------------------------------
# 6. agent_config override: per-trigger budget reaches the harness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_config_override_propagates_to_harness(
    tmp_path: Path,
) -> None:
    """Passing an agent_config bypasses the yaml loader; the override reaches the harness."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_normal_mode_payload())):
            yield msg

    override = _agent_config(latency=99, output_budget=1_234)
    await run_analyst(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_capturing_stub,
            agent_config=override,
            archive_root=tmp_path / "archive",
        )
    )

    options = captured_options[0]
    # The override's output_token_budget must reach the SDK options.
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "1234"


@pytest.mark.asyncio
async def test_default_agent_config_loaded_from_yaml(tmp_path: Path) -> None:
    """When agent_config is omitted, the runner loads the analyst entry from agents.yaml."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_normal_mode_payload())):
            yield msg

    yaml_default = load_analyst_agent_config()
    await run_analyst(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_capturing_stub,
            agent_config=None,
            archive_root=tmp_path / "archive",
        )
    )

    options = captured_options[0]
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(yaml_default.output_token_budget)


@pytest.mark.asyncio
async def test_borrow_cost_resolver_propagates_to_validation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the caller supplies a ``borrow_cost_resolver``, the runner threads
    it into :func:`build_initial_validation_state` so the validation tool sees
    the right borrow-cost lookups for short-side proposals.

    The strategist + PM consumers will rely on this contract once their work
    trees land — locking it here prevents a silent drop during a future
    runner refactor.
    """
    from alphamind.risk_guardrails.state_delivery import build_initial_validation_state

    captured_kwargs: dict[str, Any] = {}

    def _capturing_builder(**kwargs: Any) -> ValidationToolState:
        captured_kwargs.update(kwargs)
        return build_initial_validation_state(**kwargs)

    monkeypatch.setattr(
        "alphamind.decision.analyst.runner.build_initial_validation_state",
        _capturing_builder,
    )

    sentinel_resolver: Callable[[str], float] = lambda ticker: 0.0125  # noqa: E731
    await run_analyst(
        **_runner_kwargs(
            mode="normal",
            sdk_query_fn=_make_stub_query([_make_sdk_response(_normal_mode_payload())]),
            archive_root=tmp_path / "archive",
        ),
        borrow_cost_resolver=sentinel_resolver,
    )

    assert captured_kwargs["borrow_cost_resolver"] is sentinel_resolver


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


def _stub_analyst_output() -> AnalystOutput:
    return AnalystOutput(
        invocation_id=InvocationId("INV-frozen"),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        mode="normal",
        recommendations=(),
        watchlist=None,
    )


class TestAnalystResultIsFrozenDataclass:
    def test_analyst_result_is_frozen_dataclass(self) -> None:
        result = AnalystResult(
            output=_stub_analyst_output(),
            retry_count=0,
            tokens_used=_zero_tokens(),
            tool_calls_used=0,
            wall_clock_seconds=1.0,
            stop_reason="end_turn",
        )
        assert dataclasses.is_dataclass(AnalystResult)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.retry_count = 999  # type: ignore[misc]

    def test_harness_success_is_frozen_dataclass(self) -> None:
        success = HarnessSuccess(
            output=_stub_analyst_output(),
            raw_response="{}",
            retry_count=0,
            tokens_used=_zero_tokens(),
            tool_calls_used=0,
            wall_clock_seconds=1.0,
            stop_reason="end_turn",
        )
        assert dataclasses.is_dataclass(HarnessSuccess)
        with pytest.raises(dataclasses.FrozenInstanceError):
            success.retry_count = 999  # type: ignore[misc]
