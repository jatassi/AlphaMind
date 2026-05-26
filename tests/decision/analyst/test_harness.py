"""Tests for the analyst LLM invocation harness — story 07 (ALP-298).

Tests are behaviour-driven through the public interface only:
``invoke_analyst`` and the exception hierarchy. The SDK is stubbed via the
``sdk_query_fn`` dependency-injection parameter — no test calls the real
Anthropic API.
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
from alphamind.decision.analyst.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    invoke_analyst,
)
from alphamind.decision.analyst.models import AnalystOutput
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
    return {"AAPL": "tech", "NVDA": "tech", "ABC": "tech"}.get(ticker, "tech")


@pytest.fixture()
def initial_validation_state() -> ValidationToolState:
    """Build a minimal ValidationToolState the harness passes through to the MCP wrapper.

    The harness does not introspect the state; it threads it directly to
    ``build_validate_guardrail_mcp_server``. The library_config / market
    plumbing is the same shape used in the validation_tool MCP tests.
    """
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
    """Minimal BaseAgentConfig pointing to the analyst prompt."""
    return BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/analyst.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=["retrieve_brief", "validate_guardrail"],
    )


@pytest.fixture()
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def _normal_mode_payload(
    *,
    invocation_id: str = "inv-test-001",
    timestamp: str = "2026-04-28T14:30:00+00:00",
) -> dict[str, Any]:
    """Minimal valid normal-mode AnalystOutput payload — empty recommendations."""
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "mode": "normal",
        "recommendations": [],
    }


_MINIMAL_PAYLOAD = _normal_mode_payload()


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
    """Build a minimal sequence of SDK messages a stub async-generator yields.

    *structured_output* is delivered on the terminating :class:`ResultMessage`
    (the post-migration JSON-mode payload path); ``None`` simulates the SDK
    failing to populate it.
    """
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
# 1. Happy-path: invoke_analyst returns HarnessSuccess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_harness_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """invoke_analyst returns HarnessSuccess on a valid SDK response."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    result = await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-test-001",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert isinstance(result.output, AnalystOutput)
    assert result.output.mode == "normal"
    assert result.retry_count == 0
    assert result.tool_calls_used == 0
    assert result.wall_clock_seconds >= 0.0
    assert "normal" in result.raw_response
    assert isinstance(result.tokens_used, TokensUsed)
    assert result.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# 2. Architectural integration: ClaudeAgentOptions has merged MCP servers,
#    merged allowed_tools, JSON-Schema output mode, and the pinned env/extra_args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_agent_options_wires_two_mcp_servers_and_json_schema(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """ClaudeAgentOptions exposes both MCP servers, both allowed-tool names,
    JSON-Schema output mode keyed to AnalystOutput, and the pinned
    setting_sources/env/extra_args contract."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_PAYLOAD)):
            yield msg

    await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-opt-001",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=_capturing_stub,
    )

    options = captured_options[0]

    # Both MCP servers registered under their canonical server names.
    assert "alphamind_decision_validation" in options.mcp_servers
    assert "alphamind_synthesizer_retrieval" in options.mcp_servers
    val_server = options.mcp_servers["alphamind_decision_validation"]
    ret_server = options.mcp_servers["alphamind_synthesizer_retrieval"]
    assert val_server["type"] == "sdk"
    assert ret_server["type"] == "sdk"

    # allowed_tools merges both servers' wire-form names.
    expected_allowed = {
        "mcp__alphamind_decision_validation__validate_guardrail",
        "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
    }
    assert set(options.allowed_tools) == expected_allowed

    # JSON-Schema output mode targeted at AnalystOutput, with Anthropic-API
    # incompatible keys stripped (see _strip_anthropic_incompat_keys docstring
    # in harness.py — `format` and `discriminator` cause silent fallback to
    # text-output mode).
    assert options.output_format is not None
    assert options.output_format["type"] == "json_schema"
    schema = options.output_format["schema"]
    assert schema["title"] == "AnalystOutput"
    serialized = json.dumps(schema)
    assert '"format"' not in serialized, "schema must not contain `format` keys"
    assert '"discriminator"' not in serialized, "schema must not contain `discriminator` keys"
    # Validation power is preserved: the Instrument discriminated union's oneOf
    # remains, with all three variant titles reachable for the model.
    assert serialized.count('"oneOf"') >= 1
    for variant in ("InstrumentEquity", "InstrumentOption", "InstrumentStrategy"):
        assert variant in serialized, f"variant {variant} missing from schema"

    # Hardening contract: no developer settings, no built-in tools, strict MCP,
    # output-token cap pinned via env.
    assert options.tools == []
    assert options.setting_sources == []
    assert "strict-mcp-config" in options.extra_args
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(agent_config.output_token_budget)


def test_strip_anthropic_incompat_keys_preserves_property_names() -> None:
    """The strip helper is keyword-aware: a future schema with a property
    literally named ``format`` or ``discriminator`` must survive the walk.

    The stripping rule applies to JSON Schema keywords; inside ``properties``
    and ``$defs`` containers the dict keys are user-supplied names and must
    not be touched. A naive recursive strip would silently delete such a
    property — this test locks in the intended behavior.
    """
    from alphamind.decision.analyst.harness import _strip_anthropic_incompat_keys

    schema = {
        "type": "object",
        "properties": {
            "format": {"type": "string"},
            "discriminator": {"type": "integer"},
            "regular_field": {"type": "string", "format": "date-time"},
        },
        "$defs": {
            "format": {"type": "object", "properties": {"x": {"type": "string"}}},
            "discriminator": {"type": "object"},
        },
    }
    out = _strip_anthropic_incompat_keys(schema)
    # Property names preserved.
    assert set(out["properties"].keys()) == {"format", "discriminator", "regular_field"}
    # $def names preserved.
    assert set(out["$defs"].keys()) == {"format", "discriminator"}
    # Nested keyword inside a value schema is still stripped.
    assert "format" not in out["properties"]["regular_field"]
    assert out["properties"]["regular_field"]["type"] == "string"


def test_strip_anthropic_incompat_keys_removes_keywords_at_schema_level() -> None:
    """Outside named-child containers, ``format`` and ``discriminator`` keys
    are JSON Schema keywords and must be removed."""
    from alphamind.decision.analyst.harness import _strip_anthropic_incompat_keys

    schema = {
        "type": "string",
        "format": "date-time",
        "oneOf": [
            {"$ref": "#/$defs/A"},
            {"$ref": "#/$defs/B"},
        ],
        "discriminator": {"propertyName": "kind"},
    }
    out = _strip_anthropic_incompat_keys(schema)
    assert "format" not in out
    assert "discriminator" not in out
    assert out["type"] == "string"
    assert out["oneOf"] == schema["oneOf"]


# ---------------------------------------------------------------------------
# 3. Parse-failure-then-retry-success → retry_count=1 + retry message names
#    the parse contract and points at the analyst-output-schema doc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_then_retry_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A parse failure followed by a corrected response returns retry_count=1
    and emits a retry message that names the parse contract."""
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

    result = await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-test-001",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=_stub,
    )

    assert result.retry_count == 1
    assert isinstance(result.output, AnalystOutput)
    retry_prompt = captured_prompts[1]
    assert "parse contract" in retry_prompt.lower()
    assert "analyst-output-schema.md" in retry_prompt
    assert "AnalystOutput schema" in retry_prompt
    # ALP-311: original user_message must be preserved in the retry SDK call so
    # the model has portfolio/synthesizer context to repair against, not just
    # the system prompt + diagnostic.
    assert "Produce analyst output." in retry_prompt


# ---------------------------------------------------------------------------
# 4. Validation-failure-then-retry-success → retry_count=1, retry message
#    frames the structural-contract failure (not parse)
# ---------------------------------------------------------------------------


def _watchlist_payload(
    *,
    invocation_id: str = "inv-test-001",
    timestamp: str = "2026-04-28T14:30:00+00:00",
    sector: str = "tech",
) -> dict[str, Any]:
    """Watchlist-mode payload with one entry — used to trigger sector-active
    validation by narrowing ``active_sectors`` at the call site."""
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


@pytest.mark.asyncio
async def test_validation_failure_then_retry_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
) -> None:
    """A validation-error (sector not active) followed by a corrected response
    returns retry_count=1 and a retry message naming the structural contract."""
    # Narrow active sectors so the watchlist's tech entry is invalid.
    narrow_active = frozenset({"financials"})
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(_watchlist_payload(sector="tech"))):
                yield msg
        else:
            async for msg in _async_iter(
                _make_sdk_response(_watchlist_payload(sector="financials"))
            ):
                yield msg

    result = await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-test-001",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=narrow_active,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=_stub,
    )

    assert result.retry_count == 1
    assert result.output.mode == "watchlist"
    retry_prompt = captured_prompts[1]
    assert "structural contract" in retry_prompt.lower()
    # The validator emits a Rule line for validation failures.
    assert "Rule:" in retry_prompt
    assert "analyst-output-schema.md" in retry_prompt
    # ALP-311: original user_message preserved in the retry SDK call.
    assert "Produce analyst output." in retry_prompt


# ---------------------------------------------------------------------------
# 5. Validation warnings only — is_valid=True with warnings, success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_warnings_only_returns_success_no_retry(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A ValidationResult with is_valid=True and warnings (no errors) is a
    success: the harness returns HarnessSuccess with retry_count=0."""
    from alphamind.decision.analyst import harness as harness_mod
    from alphamind.decision.analyst.validation import ValidationResult, ValidationWarning

    warnings_only = ValidationResult(
        errors=(),
        warnings=(
            ValidationWarning(
                field_path="recommendations[0].time_expectation_hours",
                rule="time_horizon_consistency",
                message="window/horizon mismatch",
            ),
        ),
    )

    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_PAYLOAD)):
            yield msg

    with patch.object(harness_mod, "validate_analyst_output", return_value=warnings_only):
        result = await invoke_analyst(
            agent_config=agent_config,
            user_message="Produce analyst output.",
            invocation_id="inv-warn-only",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_stub,
        )

    assert isinstance(result, HarnessSuccess)
    assert result.retry_count == 0
    assert call_count == 1, "warnings must not trigger a retry"


# ---------------------------------------------------------------------------
# 6. Both attempts malformed → MalformedOutputFailure carrying both raw responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_attempts_malformed_raises_with_both_raw_responses(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """Two consecutive parse failures raise MalformedOutputFailure with both raw responses."""
    bad_initial = {"shape": "wrong"}
    bad_retry = {"still": "wrong"}
    stub = _make_stub_query([_make_sdk_response(bad_initial), _make_sdk_response(bad_retry)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_analyst(
            agent_config=agent_config,
            user_message="Produce analyst output.",
            invocation_id="inv-test-001",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    assert err.raw_response_initial is not None
    assert err.raw_response_retry is not None
    assert "shape" in err.raw_response_initial
    assert "still" in err.raw_response_retry
    assert err.invocation_id == "inv-test-001"
    assert err.agent_name == "analyst"


# ---------------------------------------------------------------------------
# 7. max_tokens + parse error → ContextOverflowFailure (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_with_parse_error_raises_context_overflow_no_retry(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
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
        await invoke_analyst(
            agent_config=agent_config,
            user_message="Produce analyst output.",
            invocation_id="inv-test-001",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_stub,
        )

    assert call_count == 1, "retry must not be attempted on context-overflow failure"


# ---------------------------------------------------------------------------
# 8. Slow SDK stub exceeds latency budget → TimeoutFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_sdk_stub_raises_timeout_failure(
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A slow SDK stub causes the harness to raise TimeoutFailure."""
    tight_config = BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/analyst.md",
        latency_budget_seconds=1,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=["retrieve_brief", "validate_guardrail"],
    )

    async def _slow_stub(**kwargs: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(5)
        yield  # never reached

    with pytest.raises(TimeoutFailure):
        await invoke_analyst(
            agent_config=tight_config,
            user_message="Produce analyst output.",
            invocation_id="inv-timeout-001",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_slow_stub,
        )


# ---------------------------------------------------------------------------
# 9. CLIConnectionError → SDKFailure naming CLAUDE_CODE_OAUTH_TOKEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_raises_sdk_failure_naming_env_var(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """CLIConnectionError raised by the SDK becomes SDKFailure naming the OAuth env var."""
    from claude_agent_sdk import CLIConnectionError

    async def _auth_fail_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise CLIConnectionError("OAuth token invalid or missing")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_analyst(
            agent_config=agent_config,
            user_message="Produce analyst output.",
            invocation_id="inv-auth-001",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
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
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A generic ClaudeSDKError raised by the SDK becomes SDKFailure."""
    from claude_agent_sdk import ClaudeSDKError

    async def _sdk_err_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise ClaudeSDKError("model API returned 500")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_analyst(
            agent_config=agent_config,
            user_message="Produce analyst output.",
            invocation_id="inv-sdk-001",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
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
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """A ResultMessage carrying is_error=True is surfaced as SDKFailure with the
    CLI error text in the message."""
    stub = _make_stub_query([_make_error_result(error_text="rate limit exceeded")])

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_analyst(
            agent_config=agent_config,
            user_message="Produce analyst output.",
            invocation_id="inv-cli-err-001",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )

    assert "rate limit exceeded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 12. Two-MCP-server tool-call counting — both prefixes increment the counter,
#     SDK-internal pseudo-events (other prefixes) do not
# ---------------------------------------------------------------------------


def _make_response_with_mixed_tools(
    *,
    structured_output: dict[str, Any],
    validation_calls: int,
    retrieval_calls: int,
    other_calls: int,
) -> list[Any]:
    """Build an SDK response with ToolUseBlocks across both MCP servers plus
    SDK-internal pseudo-events that must not count toward tool_calls_used."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    blocks: list[Any] = []
    for i in range(validation_calls):
        blocks.append(
            ToolUseBlock(
                id=f"v-{i}",
                name="mcp__alphamind_decision_validation__validate_guardrail",
                input={"action": "OPEN"},
            )
        )
    for i in range(retrieval_calls):
        blocks.append(
            ToolUseBlock(
                id=f"r-{i}",
                name="mcp__alphamind_synthesizer_retrieval__retrieve_brief",
                input={"ref_id": f"QR-{i}"},
            )
        )
    for i in range(other_calls):
        # Pseudo-events the SDK injects (e.g., StructuredOutput, ToolSearch)
        # must not count.
        blocks.append(ToolUseBlock(id=f"x-{i}", name="StructuredOutput", input={}))

    messages: list[Any] = [
        AssistantMessage(
            content=blocks,
            model="claude-opus-4-7",
            stop_reason=None,
            usage=None,
        ),
        AssistantMessage(
            content=[TextBlock(text="")],
            model="claude-opus-4-7",
            stop_reason="end_turn",
            usage={
                "input_tokens": 100,
                "output_tokens": 200,
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
            stop_reason="end_turn",
            usage={
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            structured_output=structured_output,
        ),
    ]
    return messages


@pytest.mark.asyncio
async def test_tool_call_counter_includes_both_mcp_prefixes_excludes_others(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """tool_calls_used counts both MCP prefixes' ToolUseBlocks; SDK pseudo-events
    (e.g., StructuredOutput) are excluded."""
    stub = _make_stub_query(
        [
            _make_response_with_mixed_tools(
                structured_output=_MINIMAL_PAYLOAD,
                validation_calls=2,
                retrieval_calls=3,
                other_calls=4,
            )
        ]
    )

    result = await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-tools-mixed",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    # 2 validation + 3 retrieval = 5; the 4 StructuredOutput pseudo-events
    # do not count.
    assert result.tool_calls_used == 5


# ---------------------------------------------------------------------------
# 13. Diagnostic archive — files written under archive_root with the
#     decision/analyst layer/agent path; retry adds response_retry.md
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_files_written_when_archive_root_provided(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """Diagnostic files written to archive_root/invocations/<id>/decision/analyst/."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-diag-001",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "2026-04-28" / "inv-diag-001" / "decision" / "analyst"
    assert (diag_dir / "prompt.md").exists()
    assert (diag_dir / "user_message.md").exists()
    assert (diag_dir / "response_initial.md").exists()
    assert (diag_dir / "errors.json").exists()
    assert (diag_dir / "metadata.json").exists()
    # No retry → no retry response file.
    assert not (diag_dir / "response_retry.md").exists()
    meta = json.loads((diag_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["tool_calls_used"] == 0
    assert meta["retry_count"] == 0
    assert meta["success"] is True


@pytest.mark.asyncio
async def test_diagnostic_files_include_retry_on_corrective_loop(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """Retry path produces response_retry.md and an error trail in errors.json."""
    stub = _make_stub_query([_make_sdk_response(None), _make_sdk_response(_MINIMAL_PAYLOAD)])

    await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-retry-001",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "2026-04-28" / "inv-retry-001" / "decision" / "analyst"
    initial_text = (diag_dir / "response_initial.md").read_text(encoding="utf-8")
    retry_text = (diag_dir / "response_retry.md").read_text(encoding="utf-8")
    assert "structured_output not populated" in initial_text
    assert "normal" in retry_text
    errors = json.loads((diag_dir / "errors.json").read_text(encoding="utf-8"))
    assert any(e.get("attempt") == 1 for e in errors)


@pytest.mark.asyncio
async def test_archive_root_none_skips_disk_io(
    agent_config: BaseAgentConfig,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    tmp_path: Path,
) -> None:
    """Passing archive_root=None skips diagnostic writes and does not crash."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    sentinel = tmp_path / "should-not-exist"
    result = await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-no-archive",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=None,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# 14. Real SDK is never invoked when sdk_query_fn is supplied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_query_fn_is_used_real_query_never_called(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """When sdk_query_fn is supplied, the real claude_agent_sdk.query is never called."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    with patch("claude_agent_sdk.query") as mock_real:
        await invoke_analyst(
            agent_config=agent_config,
            user_message="Produce analyst output.",
            invocation_id="inv-stub-001",
            initial_validation_state=initial_validation_state,
            retrieval_store=retrieval_store,
            active_sectors=active_sectors,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )
        mock_real.assert_not_called()


# ---------------------------------------------------------------------------
# 15. tokens_used accumulates across initial and retry attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tokens_used_accumulates_across_retry(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> None:
    """tokens_used and tool_calls_used sum across the initial attempt and the retry."""
    stub = _make_stub_query(
        [
            _make_sdk_response(None, input_tokens=100, output_tokens=50, tool_use_blocks=2),
            _make_sdk_response(
                _MINIMAL_PAYLOAD, input_tokens=80, output_tokens=120, tool_use_blocks=1
            ),
        ]
    )

    result = await invoke_analyst(
        agent_config=agent_config,
        user_message="Produce analyst output.",
        invocation_id="inv-cumulative",
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    assert result.retry_count == 1
    assert result.tool_calls_used == 3
    assert result.tokens_used.input_tokens == 180
    assert result.tokens_used.output_tokens == 170
