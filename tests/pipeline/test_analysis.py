"""Tests for the analysis-layer pipeline composition runner — story ALP-276.

The composition runner sequences ``run_external_distillation`` →
(``run_domain_researchers`` + ``run_qualitative_researcher``) parallel →
``run_adaptive_researcher`` → ``run_synthesizer`` and returns every
typed ``*Result`` value. All five underlying runners are monkeypatched at
the composition module's namespace so the tests do not touch SQLite, the
filesystem, or the Anthropic SDK.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from alphamind.analysis.adaptive_research.harness import SDKFailure
from alphamind.analysis.adaptive_research.runner import AdaptiveResearcherResult
from alphamind.analysis.domain_researchers.orchestrator import DomainResearchersOutput
from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult
from alphamind.analysis.synthesizer.runner import SynthesizerResult
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    BaseAgentConfig,
)
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.pipeline import analysis as composition
from alphamind.pipeline.analysis import (
    AnalysisPipelineResult,
    apply_agent_overrides,
    run_analysis_pipeline,
)
from tests.pipeline._fixtures import (
    AS_OF,
    INVOCATION_ID,
    LAST_INVOCATION_TIME,
    REGIME_LABEL_VALUE,
    StubPortfolioReader,
    _CallLog,
    build_adaptive_config,
    build_adaptive_result,
    build_agents_registry,
    build_base_config,
    build_distillation_outputs,
    build_domain_output,
    build_qualitative_result,
    build_sectors_registry,
    build_synth_result,
    patch_runners,
)

# ---------------------------------------------------------------------------
# Local aliases for backward compat within this file
# ---------------------------------------------------------------------------

_INVOCATION_ID = INVOCATION_ID
_AS_OF = AS_OF
_LAST_INVOCATION_TIME = LAST_INVOCATION_TIME
_REGIME_LABEL_VALUE = REGIME_LABEL_VALUE


def _distillation_outputs() -> DistillationOutputs:
    return build_distillation_outputs()


def _domain_output() -> DomainResearchersOutput:
    return build_domain_output()


def _qualitative_result() -> QualitativeResearcherResult:
    return build_qualitative_result()


def _adaptive_result() -> AdaptiveResearcherResult:
    return build_adaptive_result()


def _synth_result() -> SynthesizerResult:
    return build_synth_result()


def _make_base_config(prompt: str) -> BaseAgentConfig:
    return build_base_config(prompt)


def _make_adaptive_config() -> AdaptiveAgentConfig:
    return build_adaptive_config()


def _agents_registry() -> dict[str, BaseAgentConfig]:
    return build_agents_registry()


def _sectors_registry() -> dict[str, list[str]]:
    return build_sectors_registry()


# _patch_runners delegates to the shared fixture
def _patch_runners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log: _CallLog,
    distillation_result: DistillationOutputs | None = None,
    domain_result: DomainResearchersOutput | None = None,
    qualitative_result: QualitativeResearcherResult | None = None,
    adaptive_result: AdaptiveResearcherResult | None = None,
    synth_result: SynthesizerResult | None = None,
    distillation_raises: Exception | None = None,
    domain_raises: Exception | None = None,
    qualitative_raises: Exception | None = None,
    adaptive_raises: Exception | None = None,
    synth_raises: Exception | None = None,
) -> None:
    patch_runners(
        monkeypatch,
        log=log,
        distillation_result=distillation_result,
        domain_result=domain_result,
        qualitative_result=qualitative_result,
        adaptive_result=adaptive_result,
        synth_result=synth_result,
        distillation_raises=distillation_raises,
        domain_raises=domain_raises,
        qualitative_raises=qualitative_raises,
        adaptive_raises=adaptive_raises,
        synth_raises=synth_raises,
    )


def _drive(
    *,
    agents_config: Mapping[str, BaseAgentConfig] | None = None,
) -> AnalysisPipelineResult:
    """Convenience wrapper around ``asyncio.run`` for the production entry point."""
    return asyncio.run(
        run_analysis_pipeline(
            session=None,  # type: ignore[arg-type]  # stub doesn't touch the session
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            # ``to_domain()`` is called once before the stubbed
            # ``run_external_distillation`` runs (ALP-471 boundary→domain
            # projection); the stub immediately replaces that runner so the
            # projected value is never consumed.
            distillation_config=MagicMock(),
            ticker_scope=("NVDA", "JPM", "XOM"),
            universe=frozenset({"NVDA", "JPM", "XOM"}),
            agents_config=agents_config or _agents_registry(),
            sectors_config=_sectors_registry(),
            portfolio_reader=StubPortfolioReader(),
        )
    )


# ---------------------------------------------------------------------------
# apply_agent_overrides
# ---------------------------------------------------------------------------


def test_apply_agent_overrides_returns_string_keyed_mapping() -> None:
    """Output keys are the agent-name string values (the runner contract)."""
    agents = {
        AgentName.tech_semis_researcher: _make_base_config(
            "prompts/analysis/tech_semis_researcher.md"
        ),
        AgentName.adaptive_researcher: _make_adaptive_config(),
    }
    result = apply_agent_overrides(agents, agent_overrides={})
    assert set(result) == {
        AgentName.tech_semis_researcher.value,
        AgentName.adaptive_researcher.value,
    }


def test_apply_agent_overrides_layers_base_fields() -> None:
    """Override fields replace the base agent's matching fields."""
    agents = {
        AgentName.tech_semis_researcher: _make_base_config(
            "prompts/analysis/tech_semis_researcher.md"
        ),
    }
    overrides = {
        AgentName.tech_semis_researcher: {
            "latency_budget_seconds": 90,
            "output_token_budget": 8_000,
        },
    }
    result = apply_agent_overrides(agents, agent_overrides=overrides)
    cfg = result[AgentName.tech_semis_researcher.value]
    assert cfg.latency_budget_seconds == 90
    assert cfg.output_token_budget == 8_000
    # Untouched fields preserve their base value.
    assert cfg.context_token_budget == 100_000


def test_apply_agent_overrides_layers_adaptive_only_fields() -> None:
    """``cumulative_tool_*`` overrides reach the adaptive_researcher slot."""
    agents = {AgentName.adaptive_researcher: _make_adaptive_config()}
    overrides = {
        AgentName.adaptive_researcher: {
            "cumulative_tool_call_limit": 25,
            "cumulative_tool_token_budget": 5_000,
        },
    }
    result = apply_agent_overrides(agents, agent_overrides=overrides)
    cfg = result[AgentName.adaptive_researcher.value]
    assert isinstance(cfg, AdaptiveAgentConfig)
    assert cfg.cumulative_tool_call_limit == 25
    assert cfg.cumulative_tool_token_budget == 5_000


def test_apply_agent_overrides_passes_through_un_overridden_agents() -> None:
    """An agent absent from the override map yields the original config object."""
    base = _make_base_config("prompts/analysis/tech_semis_researcher.md")
    agents = {AgentName.tech_semis_researcher: base}
    result = apply_agent_overrides(agents, agent_overrides={})
    # Same object — no copy is made when no override applies.
    assert result[AgentName.tech_semis_researcher.value] is base


# ---------------------------------------------------------------------------
# run_analysis_pipeline — happy path
# ---------------------------------------------------------------------------


def test_pipeline_returns_result_on_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stage succeeds → AnalysisPipelineResult bundles every typed result."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()

    assert isinstance(result, AnalysisPipelineResult)
    assert isinstance(result.distillation_outputs, DistillationOutputs)
    assert isinstance(result.domain_researchers_output, DomainResearchersOutput)
    assert isinstance(result.qualitative_result, QualitativeResearcherResult)
    assert isinstance(result.adaptive_result, AdaptiveResearcherResult)
    assert isinstance(result.synthesizer_result, SynthesizerResult)
    assert result.synthesizer_result.synthesis_text == "synthesized prose."


def test_pipeline_threads_distillation_outputs_to_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distillation result reaches each downstream runner that needs it."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()

    # Domain orchestrator and adaptive runner both receive the DistillationOutputs.
    assert log.domain["distillation_outputs"] is log.adaptive["distillation_outputs"]
    # Qualitative receives the universal regime label dict.
    assert log.qualitative["universal_regime_label"] == log.adaptive["universal_regime_label"]
    # Adaptive's correlation_regime_brief comes from distillation outputs.
    assert (
        log.adaptive["correlation_regime_brief"]
        is log.adaptive["distillation_outputs"].correlation_regime_brief
    )


def test_pipeline_extracts_regime_label_string_for_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthesizer receives the regime-label string (not the full dict)."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    assert log.synthesizer["regime_label"] == _REGIME_LABEL_VALUE


def test_pipeline_forwards_synthesizer_agent_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthesizer slot from agents_config reaches run_synthesizer."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    agents = _agents_registry()
    _drive(agents_config=agents)
    assert log.synthesizer["agent_config"] is agents[AgentName.synthesizer.value]


def test_pipeline_forwards_briefs_to_adaptive_and_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sector + qualitative + adaptive briefs reach the right downstream stages."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()

    expected_sector_briefs = (
        result.domain_researchers_output.tech_semis.brief,
        result.domain_researchers_output.financials.brief,
        result.domain_researchers_output.energy.brief,
    )
    assert log.adaptive["sector_briefs"] == expected_sector_briefs
    assert log.synthesizer["sector_briefs"] == expected_sector_briefs
    assert log.adaptive["qualitative_brief"] is result.qualitative_result.brief
    assert log.synthesizer["qualitative_brief"] is result.qualitative_result.brief
    assert log.synthesizer["adaptive_brief"] is result.adaptive_result.brief


def test_pipeline_runs_domain_and_qualitative_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain + qualitative wall-clock approximates max(), not sum()."""

    delay = 0.10

    async def _slow_distillation(*_a: Any, **_kw: Any) -> DistillationOutputs:
        return _distillation_outputs()

    async def _slow_domain(**_kw: Any) -> DomainResearchersOutput:
        await asyncio.sleep(delay)
        return _domain_output()

    async def _slow_qualitative(*_a: Any, **_kw: Any) -> QualitativeResearcherResult:
        await asyncio.sleep(delay)
        return _qualitative_result()

    async def _stub_adaptive(*_a: Any, **_kw: Any) -> AdaptiveResearcherResult:
        return _adaptive_result()

    async def _stub_synth(**_kw: Any) -> SynthesizerResult:
        return _synth_result()

    monkeypatch.setattr(composition, "run_external_distillation", _slow_distillation)
    monkeypatch.setattr(composition, "run_domain_researchers", _slow_domain)
    monkeypatch.setattr(composition, "run_qualitative_researcher", _slow_qualitative)
    monkeypatch.setattr(composition, "run_adaptive_researcher", _stub_adaptive)
    monkeypatch.setattr(composition, "run_synthesizer", _stub_synth)

    loop = asyncio.new_event_loop()
    try:
        start = loop.time()
        loop.run_until_complete(
            run_analysis_pipeline(
                session=None,  # type: ignore[arg-type]
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                last_invocation_time=_LAST_INVOCATION_TIME,
                distillation_config=MagicMock(),
                ticker_scope=("NVDA",),
                universe=frozenset({"NVDA"}),
                agents_config=_agents_registry(),
                sectors_config=_sectors_registry(),
                portfolio_reader=StubPortfolioReader(),
            )
        )
        elapsed = loop.time() - start
    finally:
        loop.close()

    # Parallel: ~delay; serial would be ~2*delay. Generous slack accommodates CI jitter.
    assert elapsed < delay * 1.8


def test_pipeline_forwards_archive_root_and_provenance_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``archive_root`` reaches every stage; ``provenance_root`` reaches distillation."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    archive = tmp_path / "archive"
    prov = tmp_path / "provenance"
    asyncio.run(
        run_analysis_pipeline(
            session=None,  # type: ignore[arg-type]
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            distillation_config=MagicMock(),
            ticker_scope=("NVDA",),
            universe=frozenset({"NVDA"}),
            agents_config=_agents_registry(),
            sectors_config=_sectors_registry(),
            portfolio_reader=StubPortfolioReader(),
            archive_root=archive,
            provenance_root=prov,
        )
    )
    assert log.distillation["archive_root"] == archive
    assert log.distillation["provenance_root"] == prov
    assert log.domain["archive_root"] == archive
    assert log.qualitative["archive_root"] == archive
    assert log.adaptive["archive_root"] == archive
    assert log.synthesizer["archive_root"] == archive


# ---------------------------------------------------------------------------
# run_analysis_pipeline — fail-closed propagation
# ---------------------------------------------------------------------------


def _failure() -> SDKFailure:
    return SDKFailure(
        "stub failure",
        agent_name=AgentName.synthesizer.value,
        invocation_id=_INVOCATION_ID,
    )


def test_distillation_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distillation fails → no downstream stage runs."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, distillation_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert log.order == ["distillation"]


def test_domain_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Domain orchestrator fails → adaptive and synthesizer do not run."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, domain_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "adaptive" not in log.order
    assert "synthesizer" not in log.order


def test_qualitative_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qualitative fails → adaptive and synthesizer do not run."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, qualitative_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "adaptive" not in log.order
    assert "synthesizer" not in log.order


def test_adaptive_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adaptive fails → synthesizer does not run."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, adaptive_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "synthesizer" not in log.order


def test_synthesizer_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthesizer failure surfaces from ``run_analysis_pipeline``."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, synth_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure


def test_parallel_stage_double_failure_propagates_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain + qualitative both fail simultaneously → pipeline surfaces
    a single ``SDKFailure`` (the first child of the underlying
    ``BaseExceptionGroup``) rather than the group container.

    Guards the TaskGroup migration: ``asyncio.TaskGroup`` always raises
    ``BaseExceptionGroup``; without explicit unwrapping the caller would
    suddenly receive a group container instead of an ``SDKFailure``.
    """
    log = _CallLog()
    domain_failure = SDKFailure(
        "domain stub failure",
        agent_name=AgentName.tech_semis_researcher.value,
        invocation_id=_INVOCATION_ID,
    )
    qualitative_failure = SDKFailure(
        "qualitative stub failure",
        agent_name=AgentName.qualitative_researcher.value,
        invocation_id=_INVOCATION_ID,
    )
    _patch_runners(
        monkeypatch,
        log=log,
        domain_raises=domain_failure,
        qualitative_raises=qualitative_failure,
    )
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value in (domain_failure, qualitative_failure)
    # Adaptive + synthesizer must NOT have run; the orchestrator aborted at
    # the parallel stage.
    assert "adaptive" not in log.order
    assert "synthesizer" not in log.order


# ---------------------------------------------------------------------------
# Argument plumbing — qualitative + adaptive timing args
# ---------------------------------------------------------------------------


def test_pipeline_forwards_last_invocation_time_to_qualitative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qualitative receives ``last_invocation_time`` from the composition surface."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    assert log.qualitative["last_invocation_time"] == _LAST_INVOCATION_TIME


def test_pipeline_forwards_universe_to_qualitative_and_adaptive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both tool-loop agents receive the universe roster."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    expected = frozenset({"NVDA", "JPM", "XOM"})
    assert log.qualitative["universe"] == expected
    assert log.adaptive["universe"] == expected


def test_pipeline_forwards_distillation_call_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distillation receives positional session/config/scope/as_of/invocation_id."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    assert log.distillation["invocation_id"] == _INVOCATION_ID
    assert log.distillation["as_of"] == _AS_OF
    assert log.distillation["ticker_scope"] == ("NVDA", "JPM", "XOM")


def test_pipeline_forwards_agents_config_to_each_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain / qualitative / adaptive receive the same agents_config mapping."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    agents = _agents_registry()
    _drive(agents_config=agents)
    assert log.domain["agents_config"] is agents
    assert log.qualitative["agents_config"] is agents
    assert log.adaptive["agents_config"] is agents


def test_pipeline_forwards_sectors_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Domain orchestrator receives sectors_config (the only stage that needs it)."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    sectors = _sectors_registry()
    asyncio.run(
        run_analysis_pipeline(
            session=None,  # type: ignore[arg-type]
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            distillation_config=MagicMock(),
            ticker_scope=("NVDA",),
            universe=frozenset({"NVDA"}),
            agents_config=_agents_registry(),
            sectors_config=sectors,
            portfolio_reader=StubPortfolioReader(),
        )
    )
    assert log.domain["sectors_config"] is sectors


# ---------------------------------------------------------------------------
# Result type — frozen
# ---------------------------------------------------------------------------


def test_result_is_frozen_dataclass(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AnalysisPipelineResult`` rejects post-construction mutation."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.synthesizer_result = _synth_result()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Sanity-check the helper used in the production code path
# ---------------------------------------------------------------------------


def test_extract_regime_label_returns_payload_string() -> None:
    """``_extract_regime_label`` reads ``universal_regime_label['regime_label']``."""
    from alphamind.pipeline.analysis import _extract_regime_label

    outputs = _distillation_outputs()
    assert _extract_regime_label(outputs) == _REGIME_LABEL_VALUE


def test_unused_signature_carries_typing_aliases() -> None:
    """Sequence + Mapping imports stay live so static-type tooling validates them."""
    # Trivial reference so ruff/mypy don't flag the test's imports as unused.
    assert isinstance((), Sequence)
    assert isinstance({}, Mapping)
