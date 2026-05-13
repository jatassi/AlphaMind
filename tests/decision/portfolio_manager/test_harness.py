"""Tests for the portfolio-manager LLM invocation harness — story 07 (ALP-329).

Tests are behaviour-driven through the public interface only:
``invoke_pm`` and the exception hierarchy. The SDK is stubbed via the
``sdk_query_fn`` dependency-injection parameter — no test calls the real
Anthropic API. Mirrors the structure of
``tests/decision/strategist/test_harness.py``.
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

from alphamind._kernel.ids import (
    EnvelopeId,
    InvocationId,
    OrderId,
    PositionId,
    RecommendationId,
)
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.command_models import (
    BracketOrderParameters,
    EntryOrder,
    EquityInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
)
from alphamind.commands.command_models import (
    ThesisComponent as OMSThesisComponent,
)
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.decision.portfolio_manager.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    invoke_pm,
)
from alphamind.decision.portfolio_manager.models import (
    CriterionAssessment,
    OpenCommand,
    PMAnalystEnvelope,
    PMCompletionRecord,
    ThesisQualityEvaluation,
)
from alphamind.decision.portfolio_manager.submit_envelope import (
    Acknowledgment,
    FailedSubmissionEntry,
    SubmissionLogEntry,
    SubmissionResult,
    SubmitEnvelopeState,
    build_initial_submit_envelope_state,
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
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.consumers.portfolio_manager import (
    PortfolioManagerThesisComponentReader,
    PortfolioManagerView,
)
from alphamind.portfolio_state.records.theses import ThesisComponent
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
_NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_EXPIRATION_DATE = date(2026, 5, 28)
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30
_PORTFOLIO_VALUE = 100_000.0
_DEFAULT_ACTIVE_SECTORS = frozenset({"tech", "semis", "financials", "energy"})


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


def _build_validation_state() -> ValidationToolState:
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
def validation_state() -> ValidationToolState:
    return _build_validation_state()


@pytest.fixture()
def submit_envelope_state(validation_state: ValidationToolState) -> SubmitEnvelopeState:
    return build_initial_submit_envelope_state(
        invocation_id="INV-001",
        starting_validation_state=validation_state,
    )


@pytest.fixture()
def retrieval_store() -> RetrievalStore:
    return RetrievalStore(entries={}, freshness_by_source={})


@pytest.fixture()
def thesis_component_reader() -> PortfolioManagerThesisComponentReader:
    class _StubReader:
        async def get_thesis_components(self, position_id: str) -> tuple[ThesisComponent, ...]:
            return ()

    return _StubReader()


@pytest.fixture()
def pre_processor_bundle() -> ProposalPreProcessorBundle:
    """Minimal pre-processor bundle for harness tests — no proposals."""
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
        invocation_id="inv-test-001",
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


@pytest.fixture()
def pm_view() -> PortfolioManagerView:
    """Minimal PortfolioManagerView for harness tests."""
    return PortfolioManagerView.model_construct(
        positions=(),
        recent_thesis_resolutions=(),
        portfolio_pnl=None,
        drawdown=None,
        sector_exposure=(),
        directional_exposure=None,
        risk_budget=None,
        active_risk_parameters=None,
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=None,
        position_modification_trail={},
    )


@pytest.fixture()
def active_sectors() -> frozenset[str]:
    return _DEFAULT_ACTIVE_SECTORS


@pytest.fixture()
def agent_config() -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/pm.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=["retrieve_brief", "validate_guardrail", "get_thesis_components", "submit_envelope"],
    )


@pytest.fixture()
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


@pytest.fixture()
def library_config() -> LibraryConfig:
    return _config()


@pytest.fixture()
def library_market() -> MarketInputs:
    return _market()


# ---------------------------------------------------------------------------
# PMCompletionRecord payload builders
# ---------------------------------------------------------------------------


def _completion_payload(
    *,
    invocation_id: str = "inv-test-001",
    timestamp: str = "2026-04-28T14:30:00+00:00",
    envelopes_submitted: int = 1,
    approve: int = 1,
    approve_with_modification: int = 0,
    reject: int = 0,
) -> dict[str, Any]:
    """Minimal valid PMCompletionRecord payload."""
    return {
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "envelopes_submitted": envelopes_submitted,
        "verdict_summary": {
            "approve": approve,
            "approve_with_modification": approve_with_modification,
            "reject": reject,
        },
    }


_MINIMAL_PAYLOAD = _completion_payload()


def _make_sdk_response(
    structured_output: dict[str, Any] | None = None,
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
    assistant = AssistantMessage(
        content=[TextBlock(text=text)] if text else [],
        model="claude-opus-4-7",
        stop_reason=stop_reason,
        usage=usage,
    )
    result = ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason=stop_reason,
        usage=usage,
        structured_output=structured_output,
    )
    return [assistant, result]


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


def _invoke_kwargs(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
    halt_mode: bool = False,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> dict[str, Any]:
    return {
        "agent_config": agent_config,
        "user_message": user_message,
        "invocation_id": invocation_id,
        "initial_validation_state": validation_state,
        "initial_submit_envelope_state": submit_envelope_state,
        "retrieval_store": retrieval_store,
        "thesis_component_reader": thesis_component_reader,
        "pre_processor_bundle": pre_processor_bundle,
        "pm_view": pm_view,
        "active_sectors": active_sectors,
        "halt_mode": halt_mode,
        "sector_resolver": _sector_resolver,
        "library_config": library_config,
        "library_market": library_market,
        "archive_root": archive_root,
        "sdk_query_fn": sdk_query_fn,
    }


# ---------------------------------------------------------------------------
# 1. Tracer-bullet: happy path returns HarnessSuccess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_with_stub_sdk(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """invoke_pm returns HarnessSuccess on a valid SDK response."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    result = await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="Produce PM output.",
            invocation_id="inv-test-001",
            validation_state=validation_state,
            submit_envelope_state=submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )
    )

    assert isinstance(result, HarnessSuccess)
    assert isinstance(result.output, PMCompletionRecord)
    assert result.retry_count == 0
    assert isinstance(result.submission_log, tuple)
    # Sanity-check that all advertised exception classes are reachable.
    for cls in (
        ContextOverflowFailure,
        HarnessFailure,
        MalformedOutputFailure,
        SDKFailure,
        TimeoutFailure,
        SubmissionLogEntry,
    ):
        assert cls is not None


# ---------------------------------------------------------------------------
# 2. Parse failure then retry success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_retries_then_succeeds(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """A parse failure followed by a corrected response returns retry_count=1
    and emits a retry message that names the parse contract."""
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(None)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_PAYLOAD)):
                yield msg

    result = await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="Produce PM output.",
            invocation_id="inv-test-002",
            validation_state=validation_state,
            submit_envelope_state=submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=_stub,
        )
    )

    assert result.retry_count == 1
    retry_prompt = captured_prompts[1]
    assert "parse contract" in retry_prompt.lower()
    assert "PMCompletionRecord schema" in retry_prompt
    assert "pm-envelope-schema.md" in retry_prompt
    # Original user_message must be preserved in the retry SDK call.
    assert "Produce PM output." in retry_prompt


# ---------------------------------------------------------------------------
# 3. max_tokens paired with parse failure → ContextOverflowFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_with_max_tokens_raises_context_overflow(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
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
        await invoke_pm(
            **_invoke_kwargs(
                agent_config=agent_config,
                user_message="Produce PM output.",
                invocation_id="inv-test-003",
                validation_state=validation_state,
                submit_envelope_state=submit_envelope_state,
                retrieval_store=retrieval_store,
                thesis_component_reader=thesis_component_reader,
                pre_processor_bundle=pre_processor_bundle,
                pm_view=pm_view,
                active_sectors=active_sectors,
                library_config=library_config,
                library_market=library_market,
                archive_root=archive_root,
                sdk_query_fn=_stub,
            )
        )

    assert call_count == 1, "retry must not be attempted on context-overflow failure"


# ---------------------------------------------------------------------------
# 4. Both attempts malformed → MalformedOutputFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_after_retry_raises_malformed(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """Two consecutive parse failures raise MalformedOutputFailure with both raw responses."""
    bad_initial = {"shape": "wrong"}
    bad_retry = {"still": "wrong"}
    stub = _make_stub_query([_make_sdk_response(bad_initial), _make_sdk_response(bad_retry)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_pm(
            **_invoke_kwargs(
                agent_config=agent_config,
                user_message="Produce PM output.",
                invocation_id="inv-test-004",
                validation_state=validation_state,
                submit_envelope_state=submit_envelope_state,
                retrieval_store=retrieval_store,
                thesis_component_reader=thesis_component_reader,
                pre_processor_bundle=pre_processor_bundle,
                pm_view=pm_view,
                active_sectors=active_sectors,
                library_config=library_config,
                library_market=library_market,
                archive_root=archive_root,
                sdk_query_fn=stub,
            )
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    assert err.raw_response_initial is not None
    assert err.raw_response_retry is not None
    assert "shape" in err.raw_response_initial
    assert "still" in err.raw_response_retry
    assert err.invocation_id == "inv-test-004"
    assert err.agent_name == "portfolio_manager"


# ---------------------------------------------------------------------------
# 5. Slow SDK stub exceeds latency budget → TimeoutFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_raises_timeout_failure(
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """A slow SDK stub causes the harness to raise TimeoutFailure."""
    tight_config = BaseAgentConfig(
        model=AllowedModel.opus_4_7,
        prompt="prompts/decision/pm.md",
        latency_budget_seconds=1,
        context_token_budget=8_000,
        output_token_budget=4_000,
        tools=["retrieve_brief", "validate_guardrail", "get_thesis_components", "submit_envelope"],
    )

    async def _slow_stub(**kwargs: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(5)
        yield  # never reached

    with pytest.raises(TimeoutFailure):
        await invoke_pm(
            **_invoke_kwargs(
                agent_config=tight_config,
                user_message="Produce PM output.",
                invocation_id="inv-timeout-001",
                validation_state=validation_state,
                submit_envelope_state=submit_envelope_state,
                retrieval_store=retrieval_store,
                thesis_component_reader=thesis_component_reader,
                pre_processor_bundle=pre_processor_bundle,
                pm_view=pm_view,
                active_sectors=active_sectors,
                library_config=library_config,
                library_market=library_market,
                archive_root=archive_root,
                sdk_query_fn=_slow_stub,
            )
        )


# ---------------------------------------------------------------------------
# 6. Synthetic SDK exception → SDKFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_exception_raises_sdk_failure(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """A generic ClaudeSDKError raised by the SDK becomes SDKFailure."""
    from claude_agent_sdk import ClaudeSDKError

    async def _sdk_err_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise ClaudeSDKError("model API returned 500")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_pm(
            **_invoke_kwargs(
                agent_config=agent_config,
                user_message="Produce PM output.",
                invocation_id="inv-sdk-001",
                validation_state=validation_state,
                submit_envelope_state=submit_envelope_state,
                retrieval_store=retrieval_store,
                thesis_component_reader=thesis_component_reader,
                pre_processor_bundle=pre_processor_bundle,
                pm_view=pm_view,
                active_sectors=active_sectors,
                library_config=library_config,
                library_market=library_market,
                archive_root=archive_root,
                sdk_query_fn=_sdk_err_stub,
            )
        )

    err = exc_info.value
    assert isinstance(err, SDKFailure)
    assert err.cause is not None


# ---------------------------------------------------------------------------
# 7. Diagnostic archive layout — six standard files + submission_log.json
#    under archive_root/invocations/<id>/decision/portfolio_manager/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_archive_written(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """Diagnostic files are written under
    archive_root/invocations/<id>/decision/portfolio_manager/, including the
    PM-only submission_log.json alongside the standard six files."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="Produce PM output.",
            invocation_id="inv-diag-001",
            validation_state=validation_state,
            submit_envelope_state=submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )
    )

    diag_dir = archive_root / "invocations" / "inv-diag-001" / "decision" / "portfolio_manager"
    assert (diag_dir / "prompt.md").exists()
    assert (diag_dir / "user_message.md").exists()
    assert (diag_dir / "response_initial.md").exists()
    assert (diag_dir / "errors.json").exists()
    assert (diag_dir / "metadata.json").exists()
    assert (diag_dir / "submission_log.json").exists()
    assert (diag_dir / "failed_submission_log.json").exists()
    # No retry → no retry response file.
    assert not (diag_dir / "response_retry.md").exists()

    # Errors empty on the happy path.
    errors = json.loads((diag_dir / "errors.json").read_text())
    assert errors == []

    # Submission logs empty on the happy path (the stub did not invoke
    # submit_envelope from inside the SDK loop).
    log = json.loads((diag_dir / "submission_log.json").read_text())
    assert log == []
    failed_log = json.loads((diag_dir / "failed_submission_log.json").read_text())
    assert failed_log == []

    meta = json.loads((diag_dir / "metadata.json").read_text())
    assert meta["invocation_id"] == "inv-diag-001"
    assert meta["model"] == agent_config.model.value
    assert meta["retry_count"] == 0
    assert meta["success"] is True


@pytest.mark.asyncio
async def test_archive_root_none_skips_disk_io(
    agent_config: BaseAgentConfig,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
    tmp_path: Path,
) -> None:
    """Passing archive_root=None skips diagnostic writes and does not crash."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    sentinel = tmp_path / "should-not-exist"
    result = await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="Produce PM output.",
            invocation_id="inv-no-archive",
            validation_state=validation_state,
            submit_envelope_state=submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=None,
            sdk_query_fn=stub,
        )
    )

    assert isinstance(result, HarnessSuccess)
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# 8. Architectural integration: ClaudeAgentOptions wires all four MCP servers,
#    merges allowed_tools, JSON-Schema output mode targeting PMCompletionRecord
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_mcp_servers_registered(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """ClaudeAgentOptions exposes all four MCP servers, all four allowed-tool
    names, JSON-Schema output mode keyed to PMCompletionRecord, and the
    pinned setting_sources/env/extra_args contract.

    This is the architectural-integration test the parent flagged: it
    constructs the actual options and inspects the merged mcp_servers shape,
    not a stub of it. If the framework rejects the constructed options at
    runtime, this test fails before any "tool not found" issue can surface
    in production."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_PAYLOAD)):
            yield msg

    await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="Produce PM output.",
            invocation_id="inv-opt-001",
            validation_state=validation_state,
            submit_envelope_state=submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=_capturing_stub,
        )
    )

    options = captured_options[0]

    # All four MCP servers registered under their canonical server names —
    # each entry is a real McpSdkServerConfig dict, not a stub. This is the
    # load-bearing assertion the parent flagged: a stub-heavy unit test could
    # pass while the real Anthropic SDK rejects the constructed options.
    # Verifying ``type == "sdk"`` confirms ``create_sdk_mcp_server`` actually
    # ran for every factory and the framework will accept the wiring.
    expected_servers = (
        "alphamind_decision_validation",
        "alphamind_synthesizer_retrieval",
        "alphamind_portfolio_state_thesis_components",
        "alphamind_execution_oms_submit",
    )
    for server_name in expected_servers:
        assert server_name in options.mcp_servers
        assert options.mcp_servers[server_name]["type"] == "sdk"
    assert len(options.mcp_servers) == 4

    # allowed_tools merges all four servers' wire-form names.
    expected_allowed = {
        "mcp__alphamind_decision_validation__validate_guardrail",
        "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
        "mcp__alphamind_portfolio_state_thesis_components__get_thesis_components",
        "mcp__alphamind_execution_oms_submit__submit_envelope",
    }
    assert set(options.allowed_tools) == expected_allowed

    # JSON-Schema output mode targeted at PMCompletionRecord, with API-incompat
    # keys stripped.
    assert options.output_format is not None
    assert options.output_format["type"] == "json_schema"
    schema = options.output_format["schema"]
    assert schema["title"] == "PMCompletionRecord"
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
# 9. State cells are per-invocation — no state leakage across invocations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_cells_are_per_invocation(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """Calling invoke_pm twice with two distinct initial states leaves
    each invocation's state independent — neither call's MCP wiring closes
    over the other's cell."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_PAYLOAD)):
            yield msg

    state_a = _build_validation_state()
    submit_state_a = build_initial_submit_envelope_state(
        invocation_id="inv-A",
        starting_validation_state=state_a,
    )
    state_b = _build_validation_state()
    submit_state_b = build_initial_submit_envelope_state(
        invocation_id="inv-B",
        starting_validation_state=state_b,
    )

    await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="A",
            invocation_id="inv-A",
            validation_state=state_a,
            submit_envelope_state=submit_state_a,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=_capturing_stub,
        )
    )
    await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="B",
            invocation_id="inv-B",
            validation_state=state_b,
            submit_envelope_state=submit_state_b,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=_capturing_stub,
        )
    )

    options_a = captured_options[0]
    options_b = captured_options[1]
    # Each invocation builds its own MCP server map — distinct objects.
    assert options_a is not options_b
    for server_name in (
        "alphamind_decision_validation",
        "alphamind_synthesizer_retrieval",
        "alphamind_portfolio_state_thesis_components",
        "alphamind_execution_oms_submit",
    ):
        assert options_a.mcp_servers[server_name] is not options_b.mcp_servers[server_name], (
            f"server {server_name!r} leaked across invocations"
        )

    # The invocation_id baked into each submit-envelope state is preserved.
    assert submit_state_a.invocation_id == "inv-A"
    assert submit_state_b.invocation_id == "inv-B"


# ---------------------------------------------------------------------------
# 10. HarnessSuccess.submission_log threads from SubmitEnvelopeState's mutable
#     cell — covers the structural contract that the harness reads the log
#     after the SDK loop completes (acceptance criterion).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submission_log_threads_from_engine_stub_state_cell(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """The harness reads ``state.submission_log`` post-loop and threads it
    onto :class:`HarnessSuccess`. The engine-stub's MCP handler mutates the
    cell in-place during the live SDK loop; verifying the read path here
    keeps the wiring testable without driving the real Anthropic API.

    We simulate one envelope already in the log before the harness runs, and
    confirm the same tuple surfaces on ``HarnessSuccess.submission_log`` and
    in the diagnostic ``submission_log.json`` file.
    """
    eval_pass = CriterionAssessment(status="pass")
    envelope = PMAnalystEnvelope(
        envelope_id=EnvelopeId("ENV-REC-1"),
        invocation_id=InvocationId("inv-log-001"),
        source_provenance="pm_analyst",
        source_recommendation_id=RecommendationId("REC-1"),
        recommendation_type="new_entry",
        verdict="approve",
        evaluation=ThesisQualityEvaluation(
            falsifiability=eval_pass,
            sizing_proportionality=eval_pass,
            portfolio_coherence=eval_pass,
            timing_plausibility=eval_pass,
            counterargument_consideration=eval_pass,
        ),
        modifications=(),
        concerns=(),
        rationale_narrative="Test rationale.",
        anti_patterns_identified=None,
        commands=(
            OpenCommand(
                command_type="open",
                instrument=EquityInstrument(asset_type="equity", ticker="NVDA", direction="long"),
                entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
                position_size=PositionSize(quantity=10.0, dollar_value=10_000.0),
                target=Target(
                    target_type="absolute_price",
                    price=950.0,
                    pl_percentage=None,
                    pl_dollar=None,
                    order_type="limit",
                ),
                invalidation_legs=(
                    PriceLeg(
                        type="price",
                        is_hard=True,
                        condition=PriceCondition(
                            underlying_trigger="NVDA",
                            comparator="<=",
                            trigger_price=750.0,
                        ),
                        order_parameters=BracketOrderParameters(
                            order_type="market", limit_price=None
                        ),
                    ),
                ),
                thesis=Thesis(
                    summary="Long NVDA.",
                    components=(
                        OMSThesisComponent(
                            component_type="entry_rationale",
                            linked_leg="entry",
                            instrument_reference="NVDA",
                            narrative="Capex tailwind.",
                            key_assumptions=("Capex stays elevated.",),
                        ),
                    ),
                ),
            ),
        ),
    )
    fake_entry = SubmissionLogEntry(
        envelope=envelope,
        submission_results=(
            SubmissionResult(
                command_ordinal=0,
                status="accepted",
                command_id="inv-log-001.ENV-REC-1.0.0",
                acknowledgment=Acknowledgment(
                    position_id=PositionId("POS-NVDA-stub"), order_id=OrderId("ORD-NVDA-stub")
                ),
            ),
        ),
    )
    submit_envelope_state.submission_log = (fake_entry,)

    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])
    result = await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="Produce PM output.",
            invocation_id="inv-log-001",
            validation_state=validation_state,
            submit_envelope_state=submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )
    )

    assert result.submission_log == (fake_entry,)

    diag_dir = archive_root / "invocations" / "inv-log-001" / "decision" / "portfolio_manager"
    log_dump = json.loads((diag_dir / "submission_log.json").read_text())
    assert len(log_dump) == 1
    assert log_dump[0]["envelope"]["envelope_id"] == "ENV-REC-1"
    assert log_dump[0]["submission_results"][0]["status"] == "accepted"


# ---------------------------------------------------------------------------
# 10b. failed_submission_log.json captures Layer-1 parse failures from the
#      engine-stub state cell.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_submission_log_archived_from_state_cell(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """The harness writes ``state.failed_submission_log`` to
    ``failed_submission_log.json`` post-loop so Layer-1 parse failures are
    preserved in the archive even though they never produced a parsed
    envelope (ALP-353)."""
    fake_failure = FailedSubmissionEntry(
        raw_args={"envelope_id": "ENV-REC-99", "garbage": "value"},
        validation_error_repr="1 validation error for PMEnvelope\nsource_provenance: missing",
        command_id="inv-fail-001.ENV-REC-99.0.0",
    )
    submit_envelope_state.failed_submission_log = (fake_failure,)

    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])
    await invoke_pm(
        **_invoke_kwargs(
            agent_config=agent_config,
            user_message="Produce PM output.",
            invocation_id="inv-fail-001",
            validation_state=validation_state,
            submit_envelope_state=submit_envelope_state,
            retrieval_store=retrieval_store,
            thesis_component_reader=thesis_component_reader,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            library_config=library_config,
            library_market=library_market,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )
    )

    diag_dir = archive_root / "invocations" / "inv-fail-001" / "decision" / "portfolio_manager"
    failed_dump = json.loads((diag_dir / "failed_submission_log.json").read_text())
    assert len(failed_dump) == 1
    assert failed_dump[0]["command_id"] == "inv-fail-001.ENV-REC-99.0.0"
    assert failed_dump[0]["raw_args"] == {"envelope_id": "ENV-REC-99", "garbage": "value"}
    assert "source_provenance" in failed_dump[0]["validation_error_repr"]


# ---------------------------------------------------------------------------
# 11. Real SDK is never invoked when sdk_query_fn is supplied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_query_fn_is_used_real_query_never_called(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    validation_state: ValidationToolState,
    submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> None:
    """When sdk_query_fn is supplied, the real claude_agent_sdk.query is never called."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_PAYLOAD)])

    with patch("claude_agent_sdk.query") as mock_real:
        await invoke_pm(
            **_invoke_kwargs(
                agent_config=agent_config,
                user_message="Produce PM output.",
                invocation_id="inv-stub-only",
                validation_state=validation_state,
                submit_envelope_state=submit_envelope_state,
                retrieval_store=retrieval_store,
                thesis_component_reader=thesis_component_reader,
                pre_processor_bundle=pre_processor_bundle,
                pm_view=pm_view,
                active_sectors=active_sectors,
                library_config=library_config,
                library_market=library_market,
                archive_root=archive_root,
                sdk_query_fn=stub,
            )
        )
        mock_real.assert_not_called()
