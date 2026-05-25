"""Tests for the strategist LLM invocation harness — story 06 (ALP-307).

Tests are behaviour-driven through the public interface only:
``run_strategist_harness`` and the exception hierarchy. The SDK is stubbed via
the ``sdk_query_fn`` dependency-injection parameter — no test calls the real
Anthropic API. Mirrors the structure of
``tests/decision/analyst/test_harness.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import pytest

from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.decision.strategist.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    run_strategist_harness,
)
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
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
from alphamind.risk_guardrails.state_delivery.validation_tool import ValidationToolState

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXPIRATION_DATE = date(2026, 5, 28)
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30
_PORTFOLIO_VALUE = 100_000.0


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _config() -> LibraryConfig:
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


def _snapshot(
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


def _atm_provider(underlying: str = "AAPL") -> FixtureIvProvider:
    return FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
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
    )


def _market(underlyings: Sequence[str] = ("AAPL", "NVDA", "ABC")) -> MarketInputs:
    return MarketInputs(
        underlying_prices=MappingProxyType({u: _SPOT for u in underlyings}),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=_atm_provider("AAPL"),
        as_of=_AS_OF,
    )


def _risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _active_risk_parameters() -> ActiveRiskParameterSet:
    from tests.decision.conftest import compose_active_risk_parameters_via_orchestrator

    return compose_active_risk_parameters_via_orchestrator(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(),
        active_overlays=(),
    )


def _sector_resolver(ticker: str) -> str:
    return {"AAPL": "tech", "NVDA": "semis", "ABC": "tech"}.get(ticker, "tech")


@pytest.fixture()
def validation_state() -> ValidationToolState:
    """Build a minimal ValidationToolState the harness threads to the MCP wrapper."""
    cfg = _config()
    return ValidationToolState(
        invocation_id="INV-001",
        starting_snapshot=_snapshot(),
        starting_risk_budget=_risk_budget(),
        starting_active_risk_parameters=_active_risk_parameters(),
        profile_feature_flags=cfg.feature_flags,
        library_config=cfg,
        library_market=_market(),
        sector_resolver=_sector_resolver,
        accumulated_deltas=(),
    )


@pytest.fixture()
def retrieval_store() -> RetrievalStore:
    """Empty retrieval store — no Layer-3 references in the test payloads."""
    return RetrievalStore(entries={}, freshness_by_source={})


@pytest.fixture()
def active_sectors() -> frozenset[str]:
    return frozenset({"tech", "semis", "financials", "energy"})


@pytest.fixture()
def agent_config() -> BaseAgentConfig:
    """Minimal BaseAgentConfig pointing to the strategist prompt."""
    return BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/strategist.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=["retrieve_brief", "validate_guardrail"],
    )


@pytest.fixture()
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


# ---------------------------------------------------------------------------
# StrategistOutput payload builders
# ---------------------------------------------------------------------------


def _normal_payload(
    *,
    invocation_id: str = "inv-test-001",
    timestamp: str = "2026-04-28T14:30:00+00:00",
    sector: str = "semis",
) -> dict[str, Any]:
    """Minimal valid normal-mode StrategistOutput payload."""
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "mode": "normal",
        "position_assessments": [
            {
                "assessment_id": "SA-1",
                "position_id": "POS-NVDA-001",
                "thesis_id": "TH-NVDA-001",
                "underlying": "NVDA",
                "sector": sector,
                "thesis_status": "on-track",
                "prior_status": "on-track",
                "recommended_action": "hold",
                "status_rationale": "Thesis remains supported.",
                "action_rationale": "No signal to change; hold.",
            }
        ],
        "pending_order_assessments": [],
        "portfolio_level_observations": {
            "aggregate_thesis_health": "Single open position; on-track.",
            "sector_balance_shifts": "No material sector shift.",
            "thesis_dependency_warnings": "No correlated breakdowns.",
            "capital_allocation_observations": "Capital efficient at current sizing.",
        },
    }


def _defensive_payload(
    *,
    invocation_id: str = "inv-defensive-001",
    timestamp: str = "2026-04-28T14:30:00+00:00",
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


_MINIMAL_PAYLOAD = _normal_payload()


def _make_sdk_response(
    structured_output: dict[str, Any] | None = None,
    *,
    text: str = "",
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
    tool_use_blocks: int = 0,
    tool_use_block_name: str = "mcp__alphamind_decision_validation__validate_guardrail",
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    messages: list[Any] = []

    if tool_use_blocks:
        tool_blocks: list[Any] = [
            ToolUseBlock(id=f"tu-{i}", name=tool_use_block_name, input={"ref_id": "QR-1"})
            for i in range(tool_use_blocks)
        ]
        messages.append(
            AssistantMessage(
                content=tool_blocks,
                model="claude-opus-4-7",
                stop_reason=None,
                usage=None,
            )
        )

    assistant = AssistantMessage(
        content=[TextBlock(text=text)] if text else [],
        model="claude-opus-4-7",
        stop_reason=stop_reason,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )
    result = ResultMessage(
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
    )
    messages.extend([assistant, result])
    return messages


def _make_error_result(
    *,
    error_text: str = "CLI error",
    stop_reason: str | None = "end_turn",
) -> list[Any]:
    """Build a sequence ending in a ResultMessage with is_error=True."""
    from claude_agent_sdk import ResultMessage

    return [
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=True,
            num_turns=1,
            session_id="sess-err",
            stop_reason=stop_reason,
            usage={
                "input_tokens": 10,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            result=error_text,
        )
    ]


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _make_stub_query(responses: list[list[Any]]) -> Callable[..., AsyncGenerator[Any]]:
    """Return a stub sdk_query_fn that yields successive responses."""
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        async for msg in _async_iter(responses[idx]):
            yield msg

    return _stub


# ---------------------------------------------------------------------------
# 1. Tracer-bullet: happy path returns HarnessSuccess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_harness_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """run_strategist_harness returns HarnessSuccess on a valid SDK response."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    result = await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-test-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert isinstance(result.output, StrategistOutput)
    assert result.output.mode == "normal"
    assert isinstance(result.tokens_used, TokensUsed)
    assert result.metadata["model"] == agent_config.model.value
    assert result.metadata["attempts"] == 1
    assert result.validation_result.is_valid


# ---------------------------------------------------------------------------
# 2. Happy path: defensive_posture mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_defensive_posture_mode(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """run_strategist_harness handles a defensive_posture-mode payload."""
    stub = _make_stub_query([_make_sdk_response(_defensive_payload())])

    result = await run_strategist_harness(
        user_message="Defensive posture invocation.",
        system_prompt="You are the strategist.",
        invocation_id="inv-defensive-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.output.mode == "defensive_posture"
    assert result.metadata["mode"] == "defensive_posture"


# ---------------------------------------------------------------------------
# 3. Architectural integration: ClaudeAgentOptions wires both MCP servers,
#    merges allowed_tools, and uses JSON-Schema output mode targeting
#    StrategistOutput
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_wires_two_mcp_servers_and_json_schema(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """ClaudeAgentOptions exposes both MCP servers, both allowed-tool names,
    JSON-Schema output mode keyed to StrategistOutput, and the pinned
    setting_sources/env/extra_args contract."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_PAYLOAD)):
            yield msg

    await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-opt-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    options = captured_options[0]

    # Both MCP servers registered under their canonical server names.
    assert "alphamind_decision_validation" in options.mcp_servers
    assert "alphamind_synthesizer_retrieval" in options.mcp_servers

    # allowed_tools merges both servers' wire-form names — the validation
    # MCP exposes both the single-call and batch tools (ALP-625).
    expected_allowed = {
        "mcp__alphamind_decision_validation__validate_guardrail",
        "mcp__alphamind_decision_validation__validate_guardrail_batch",
        "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
    }
    assert set(options.allowed_tools) == expected_allowed

    # JSON-Schema output mode targeted at StrategistOutput, with API-incompat
    # keys stripped.
    assert options.output_format is not None
    assert options.output_format["type"] == "json_schema"
    schema = options.output_format["schema"]
    assert schema["title"] == "StrategistOutput"
    serialized = json.dumps(schema)
    assert '"format"' not in serialized, "schema must not contain `format` keys"
    assert '"discriminator"' not in serialized, "schema must not contain `discriminator` keys"

    # Hardening contract: no developer settings, no built-in tools, strict MCP,
    # output-token cap pinned via env.
    assert options.tools == []
    assert options.setting_sources == []
    assert "strict-mcp-config" in options.extra_args
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(agent_config.output_token_budget)


# ---------------------------------------------------------------------------
# 4. Parse-failure-then-retry-success → metadata.attempts=2, retry message
#    names the parse contract and points at the strategist-output-schema doc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_then_retry_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A parse failure followed by a corrected response returns attempts=2 and
    emits a retry message that names the parse contract."""
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            # ``structured_output=None`` simulates the SDK failing to populate
            # the field — a parse-stage failure that triggers the retry path.
            async for msg in _async_iter(_make_sdk_response(None)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_PAYLOAD)):
                yield msg

    result = await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-test-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert result.metadata["attempts"] == 2
    retry_prompt = captured_prompts[1]
    assert "parse contract" in retry_prompt.lower()
    assert "strategist-output-schema.md" in retry_prompt
    assert "StrategistOutput schema" in retry_prompt
    # ALP-311: original user_message must be preserved in the retry SDK call so
    # the model has portfolio/synthesizer context to repair against, not just
    # the system prompt + diagnostic.
    assert "Produce strategist output." in retry_prompt


# ---------------------------------------------------------------------------
# 5. Validation-failure-then-retry-success → attempts=2, retry message frames
#    the structural-contract failure (not parse)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_then_retry_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
) -> None:
    """A validation-error (sector not active) followed by a corrected response
    returns attempts=2 and a retry message naming the structural contract."""
    # Narrow active sectors so the position assessment with sector=semis is invalid.
    narrow_active = frozenset({"financials"})
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(_normal_payload(sector="semis"))):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_normal_payload(sector="financials"))):
                yield msg

    result = await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-test-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=narrow_active,
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert result.metadata["attempts"] == 2
    retry_prompt = captured_prompts[1]
    assert "structural contract" in retry_prompt.lower()
    assert "Rule:" in retry_prompt
    assert "strategist-output-schema.md" in retry_prompt
    # ALP-311: original user_message preserved in the retry SDK call.
    assert "Produce strategist output." in retry_prompt


# ---------------------------------------------------------------------------
# 6. Both attempts malformed → MalformedOutputFailure carrying both raw
#    responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_attempts_malformed_raises_with_both_raw_responses(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """Two consecutive parse failures raise MalformedOutputFailure with both raw responses."""
    bad_initial = {"shape": "wrong"}
    bad_retry = {"still": "wrong"}
    stub = _make_stub_query([_make_sdk_response(bad_initial), _make_sdk_response(bad_retry)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await run_strategist_harness(
            user_message="Produce strategist output.",
            system_prompt="You are the strategist.",
            invocation_id="inv-test-001",
            agent_config=agent_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    assert err.raw_response_initial is not None
    assert err.raw_response_retry is not None
    assert "shape" in err.raw_response_initial
    assert "still" in err.raw_response_retry
    assert err.invocation_id == "inv-test-001"
    assert err.agent_name == "strategist"


# ---------------------------------------------------------------------------
# 7. max_tokens + parse error → ContextOverflowFailure (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_with_parse_error_raises_context_overflow_no_retry(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """Parse failure paired with stop_reason=max_tokens raises
    ContextOverflowFailure immediately without attempting a retry."""
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        async for msg in _async_iter(_make_sdk_response(None, stop_reason="max_tokens")):
            yield msg

    with pytest.raises(ContextOverflowFailure):
        await run_strategist_harness(
            user_message="Produce strategist output.",
            system_prompt="You are the strategist.",
            invocation_id="inv-test-001",
            agent_config=agent_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            sdk_query_fn=_stub,
        )

    assert call_count == 1, "retry must not be attempted on context-overflow failure"


# ---------------------------------------------------------------------------
# 8. Slow SDK stub exceeds latency budget → TimeoutFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_sdk_stub_raises_timeout_failure(
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A slow SDK stub causes the harness to raise TimeoutFailure."""
    tight_config = BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/strategist.md",
        latency_budget_seconds=1,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=["retrieve_brief", "validate_guardrail"],
    )

    async def _slow_stub(**kwargs: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(5)
        yield  # never reached

    with pytest.raises(TimeoutFailure):
        await run_strategist_harness(
            user_message="Produce strategist output.",
            system_prompt="You are the strategist.",
            invocation_id="inv-timeout-001",
            agent_config=tight_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            sdk_query_fn=_slow_stub,
        )


# ---------------------------------------------------------------------------
# 9. CLIConnectionError → SDKFailure naming CLAUDE_CODE_OAUTH_TOKEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_raises_sdk_failure_naming_env_var(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """CLIConnectionError raised by the SDK becomes SDKFailure naming the OAuth env var."""
    from claude_agent_sdk import CLIConnectionError

    async def _auth_fail_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise CLIConnectionError("OAuth token invalid or missing")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await run_strategist_harness(
            user_message="Produce strategist output.",
            system_prompt="You are the strategist.",
            invocation_id="inv-auth-001",
            agent_config=agent_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            sdk_query_fn=_auth_fail_stub,
        )

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 10. ClaudeSDKError → SDKFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_sdk_error_raises_sdk_failure(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A generic ClaudeSDKError raised by the SDK becomes SDKFailure."""
    from claude_agent_sdk import ClaudeSDKError

    async def _sdk_err_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise ClaudeSDKError("model API returned 500")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await run_strategist_harness(
            user_message="Produce strategist output.",
            system_prompt="You are the strategist.",
            invocation_id="inv-sdk-001",
            agent_config=agent_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            sdk_query_fn=_sdk_err_stub,
        )

    err = exc_info.value
    assert isinstance(err, SDKFailure)
    assert err.cause is not None


# ---------------------------------------------------------------------------
# 11. ResultMessage.is_error=True → SDKFailure with CLI-error context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_result_error_raises_sdk_failure(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A ResultMessage carrying is_error=True is surfaced as SDKFailure."""
    stub = _make_stub_query([_make_error_result(error_text="rate limit exceeded")])

    with pytest.raises(SDKFailure) as exc_info:
        await run_strategist_harness(
            user_message="Produce strategist output.",
            system_prompt="You are the strategist.",
            invocation_id="inv-cli-err-001",
            agent_config=agent_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )

    assert "rate limit exceeded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 12. Diagnostic archive layout — files written under
#     archive_root/invocations/<id>/decision/strategist/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_files_written_when_archive_root_provided(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """Diagnostic files written to archive_root/invocations/<id>/decision/strategist/."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-diag-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "invocations" / "inv-diag-001" / "decision" / "strategist"
    assert (diag_dir / "prompt.md").exists()
    assert (diag_dir / "user_message.md").exists()
    assert (diag_dir / "response_initial.md").exists()
    assert (diag_dir / "errors.json").exists()
    assert (diag_dir / "metadata.json").exists()
    # No retry → no retry response file.
    assert not (diag_dir / "response_retry.md").exists()
    # Errors list is empty on the happy path.
    errors = json.loads((diag_dir / "errors.json").read_text(encoding="utf-8"))
    assert errors == []
    meta = json.loads((diag_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["invocation_id"] == "inv-diag-001"
    assert meta["mode"] == "normal"
    assert meta["model"] == agent_config.model.value
    assert meta["attempts"] == 1
    assert "agent_config_snapshot" in meta
    assert meta["agent_config_snapshot"]["model"] == agent_config.model.value
    assert meta["success"] is True


@pytest.mark.asyncio
async def test_diagnostic_files_include_retry_on_corrective_loop(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """Retry path produces response_retry.md and an error trail in errors.json."""
    stub = _make_stub_query([_make_sdk_response(None), _make_sdk_response(_MINIMAL_PAYLOAD)])

    await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-retry-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "invocations" / "inv-retry-001" / "decision" / "strategist"
    assert (diag_dir / "response_retry.md").exists()
    initial_text = (diag_dir / "response_initial.md").read_text(encoding="utf-8")
    retry_text = (diag_dir / "response_retry.md").read_text(encoding="utf-8")
    assert "structured_output not populated" in initial_text
    assert "normal" in retry_text
    errors = json.loads((diag_dir / "errors.json").read_text(encoding="utf-8"))
    assert any(e.get("attempt") == 1 and e.get("kind") == "parse" for e in errors)


@pytest.mark.asyncio
async def test_archive_root_none_skips_disk_io(
    agent_config: BaseAgentConfig,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    tmp_path: Path,
) -> None:
    """Passing archive_root=None skips diagnostic writes and does not crash."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    sentinel = tmp_path / "should-not-exist"
    result = await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-no-archive",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=None,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# 13. Real SDK is never invoked when sdk_query_fn is supplied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_query_fn_is_used_real_query_never_called(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """When sdk_query_fn is supplied, the real claude_agent_sdk.query is never called."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    with patch("claude_agent_sdk.query") as mock_real:
        await run_strategist_harness(
            user_message="Produce strategist output.",
            system_prompt="You are the strategist.",
            invocation_id="inv-stub-001",
            agent_config=agent_config,
            validation_state=validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )
        mock_real.assert_not_called()


# ---------------------------------------------------------------------------
# 14. Metadata dict carries all story-06 § 6 fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_carries_story_06_fields(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """HarnessSuccess.metadata carries invocation_id, mode, model,
    agent_config_snapshot, total_tokens, attempts."""
    stub = _make_stub_query(
        [_make_sdk_response(_MINIMAL_PAYLOAD, input_tokens=120, output_tokens=80)]
    )

    result = await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-meta-001",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.metadata["invocation_id"] == "inv-meta-001"
    assert result.metadata["mode"] == "normal"
    assert result.metadata["model"] == agent_config.model.value
    snapshot = result.metadata["agent_config_snapshot"]
    assert snapshot["latency_budget_seconds"] == agent_config.latency_budget_seconds
    assert snapshot["output_token_budget"] == agent_config.output_token_budget
    assert result.metadata["total_tokens"] == 200  # 120 + 80
    assert result.metadata["attempts"] == 1


# ---------------------------------------------------------------------------
# 15. Tokens accumulate across initial + retry attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_used_accumulates_across_retry(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """tokens_used sums across the initial attempt and the retry."""
    stub = _make_stub_query(
        [
            _make_sdk_response(None, input_tokens=100, output_tokens=50),
            _make_sdk_response(_MINIMAL_PAYLOAD, input_tokens=80, output_tokens=120),
        ]
    )

    result = await run_strategist_harness(
        user_message="Produce strategist output.",
        system_prompt="You are the strategist.",
        invocation_id="inv-cumulative",
        agent_config=agent_config,
        validation_state=validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.metadata["attempts"] == 2
    assert result.tokens_used.input_tokens == 180
    assert result.tokens_used.output_tokens == 170
