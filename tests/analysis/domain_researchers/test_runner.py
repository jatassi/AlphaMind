"""Tests for the domain-researcher runner — story 10 (ALP-191).

All composed dependencies (qualitative loader, bundle assembler, harness) are
injected via the private ``_run_domain_researcher`` function so tests don't
touch the Anthropic API, the database, or the filesystem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.harness import (
    HarnessSuccess,
    MalformedOutputFailure,
)
from alphamind.analysis.domain_researchers.input_bundle import InputBundle, assemble_input_bundle
from alphamind.analysis.domain_researchers.models import SECTOR_PREFIX, SectorBrief, SignalQuality
from alphamind.analysis.domain_researchers.qualitative_input import SectorQualitativeInput
from alphamind.analysis.domain_researchers.runner import (
    DomainResearcherResult,
    _Deps,
    _run_domain_researcher,
)
from alphamind.config.models.agents import AgentName, AllowedModel, BaseAgentConfig

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "test-inv-001"


def _make_agent_config() -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/tech_semis_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )


def _make_agents_registry() -> dict[str, BaseAgentConfig]:
    """A minimal agents registry with all three sector researcher entries."""
    cfg = _make_agent_config()
    return {
        AgentName.tech_semis_researcher.value: cfg,
        AgentName.financials_researcher.value: cfg,
        AgentName.energy_researcher.value: cfg,
    }


def _make_sectors_registry() -> dict[str, list[str]]:
    """A minimal sectors registry matching AssetsConfig.sectors shape."""
    return {
        Sector.TECH_SEMIS.value: ["NVDA", "AMD", "INTC"],
        Sector.FINANCIALS.value: ["JPM", "GS", "BAC"],
        Sector.ENERGY.value: ["XOM", "CVX", "SLB"],
    }


def _make_qualitative_input(sector: Sector) -> SectorQualitativeInput:
    """Build a minimal SectorQualitativeInput (no headlines or events)."""
    return SectorQualitativeInput(
        sector=sector,
        as_of=_AS_OF,
        lookback_window_hours=24,
        headlines=(),
        events=(),
        data_freshness=_AS_OF,
    )


def _make_sector_brief(sector: Sector) -> SectorBrief:
    """Build a minimal SectorBrief with one finding."""
    from alphamind.analysis.domain_researchers.models import (
        Finding,
        SignalType,
        Strength,
    )

    prefix = SECTOR_PREFIX[sector]
    return SectorBrief(
        invocation_id=_INVOCATION_ID,
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline="Test finding",
                tickers=("NVDA",),
                signal_type=SignalType.PRICE_ACTION,
                strength=Strength.STRONG,
                detail="Test detail.",
            ),
        ),
        anomalies=(),
        thesis_candidates=(),
    )


def _make_harness_success(sector: Sector) -> HarnessSuccess:
    return HarnessSuccess(
        brief=_make_sector_brief(sector),
        raw_response="raw text",
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=100,
            output_tokens=200,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        wall_clock_seconds=1.5,
    )


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------


def test_run_domain_researcher_returns_result() -> None:
    """Happy path: stubs return canned values → DomainResearcherResult returned."""
    sector = Sector.TECH_SEMIS

    result = asyncio.run(
        _run_domain_researcher(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_output_text="distillation text",
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            deps=_make_deps(sector),
        )
    )

    assert isinstance(result, DomainResearcherResult)
    assert result.sector == sector
    assert result.brief.invocation_id == _INVOCATION_ID
    assert len(result.brief.findings) == 1


# ---------------------------------------------------------------------------
# Shared async stub helpers
# ---------------------------------------------------------------------------


def _make_stub_harness(result: HarnessSuccess) -> Callable[..., Any]:
    """Return an async callable that always returns *result*."""

    async def _stub(**kw: object) -> HarnessSuccess:
        return result

    return _stub


def _make_stub_qualitative_loader(
    qi: SectorQualitativeInput,
) -> Callable[..., SectorQualitativeInput]:
    """Return a sync callable that ignores all args and returns *qi*."""

    def _stub(**kw: object) -> SectorQualitativeInput:
        return qi

    return _stub


def _make_deps(sector: Sector) -> _Deps:
    """Build a standard _Deps bundle with stubs for a given sector."""
    return _Deps(
        qualitative_loader=_make_stub_qualitative_loader(_make_qualitative_input(sector)),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_make_stub_harness(_make_harness_success(sector)),
    )


# ---------------------------------------------------------------------------
# Test 2: Config-drift raises ValueError
# ---------------------------------------------------------------------------


def test_config_drift_raises_value_error() -> None:
    """When the expected agent is absent from agents_config, ValueError is raised."""
    sector = Sector.TECH_SEMIS
    agents_config = {
        AgentName.financials_researcher.value: _make_agent_config(),
        AgentName.energy_researcher.value: _make_agent_config(),
        # tech_semis_researcher intentionally absent — simulates config drift
    }

    with pytest.raises(ValueError, match="config drift"):
        asyncio.run(
            _run_domain_researcher(
                sector=sector,
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                distillation_output_text="distillation text",
                agents_config=agents_config,
                sectors_config=_make_sectors_registry(),
                deps=_make_deps(sector),
            )
        )


# ---------------------------------------------------------------------------
# Test 3: HarnessFailure propagates unchanged
# ---------------------------------------------------------------------------


def test_harness_failure_propagates_unchanged() -> None:
    """A HarnessFailure raised by the harness propagates up without wrapping."""
    sector = Sector.TECH_SEMIS
    original_failure = MalformedOutputFailure(
        "parse failed on both attempts",
        agent_name=AgentName.tech_semis_researcher.value,
        invocation_id=_INVOCATION_ID,
    )

    async def _failing_harness(**kw: object) -> HarnessSuccess:
        raise original_failure

    qi = _make_qualitative_input(sector)
    deps = _Deps(
        qualitative_loader=_make_stub_qualitative_loader(qi),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_failing_harness,
    )

    with pytest.raises(MalformedOutputFailure) as exc_info:
        asyncio.run(
            _run_domain_researcher(
                sector=sector,
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                distillation_output_text="distillation text",
                agents_config=_make_agents_registry(),
                sectors_config=_make_sectors_registry(),
                deps=deps,
            )
        )

    assert exc_info.value is original_failure


# ---------------------------------------------------------------------------
# Test 4: Composition — qualitative loader output passed verbatim to bundle assembler
# ---------------------------------------------------------------------------


def test_bundle_assembler_receives_qualitative_loader_output_verbatim() -> None:
    """The bundle assembler receives exactly what the qualitative loader returned."""
    sector = Sector.FINANCIALS
    expected_qi = _make_qualitative_input(sector)
    received_qi: list[SectorQualitativeInput] = []

    def _capturing_bundle_assembler(**kw: object) -> InputBundle:
        received_qi.append(kw["qualitative_input"])  # type: ignore[arg-type]
        return assemble_input_bundle(**kw)  # type: ignore[arg-type]

    deps = _Deps(
        qualitative_loader=_make_stub_qualitative_loader(expected_qi),
        bundle_assembler=_capturing_bundle_assembler,
        harness_fn=_make_stub_harness(_make_harness_success(sector)),
    )
    asyncio.run(
        _run_domain_researcher(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_output_text="distillation text",
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            deps=deps,
        )
    )

    assert len(received_qi) == 1
    assert received_qi[0] is expected_qi


# ---------------------------------------------------------------------------
# Test 5: Harness receives assembled bundle text verbatim
# ---------------------------------------------------------------------------


def test_harness_receives_bundle_text_verbatim() -> None:
    """The harness user_message kwarg equals the bundle_text from the assembler."""
    sector = Sector.ENERGY
    received_messages: list[str] = []

    async def _capturing_harness(**kw: object) -> HarnessSuccess:
        received_messages.append(str(kw["user_message"]))
        return _make_harness_success(sector)

    deps = _Deps(
        qualitative_loader=_make_stub_qualitative_loader(_make_qualitative_input(sector)),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_capturing_harness,
    )
    result = asyncio.run(
        _run_domain_researcher(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_output_text="distillation text",
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            deps=deps,
        )
    )

    assert len(received_messages) == 1
    assert received_messages[0] == result.input_bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 6: Sector membership map has exactly three sector entries
# ---------------------------------------------------------------------------


def test_sector_membership_map_has_three_entries() -> None:
    """The runner builds a sector membership map covering all three sectors."""
    sector = Sector.TECH_SEMIS

    result = asyncio.run(
        _run_domain_researcher(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_output_text="distillation text",
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            deps=_make_deps(sector),
        )
    )
    assert result.sector == sector

    # Negative: sectors_config missing one sector raises KeyError
    incomplete_sectors_config = {
        Sector.TECH_SEMIS.value: ["NVDA"],
        Sector.FINANCIALS.value: ["JPM"],
        # ENERGY intentionally absent
    }
    with pytest.raises(KeyError):
        asyncio.run(
            _run_domain_researcher(
                sector=sector,
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                distillation_output_text="distillation text",
                agents_config=_make_agents_registry(),
                sectors_config=incomplete_sectors_config,
                deps=_make_deps(sector),
            )
        )


# ---------------------------------------------------------------------------
# Test 7: Composition order: qualitative → bundle → harness
# ---------------------------------------------------------------------------


def test_composition_order() -> None:
    """Steps execute in order: qualitative load → bundle assemble → harness invoke."""
    sector = Sector.TECH_SEMIS
    call_order: list[str] = []

    def _loader(**kw: object) -> SectorQualitativeInput:
        call_order.append("qualitative")
        return _make_qualitative_input(sector)

    def _assembler(**kw: object) -> InputBundle:
        call_order.append("bundle")
        return assemble_input_bundle(**kw)  # type: ignore[arg-type]

    async def _harness(**kw: object) -> HarnessSuccess:
        call_order.append("harness")
        return _make_harness_success(sector)

    asyncio.run(
        _run_domain_researcher(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_output_text="distillation text",
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            deps=_Deps(
                qualitative_loader=_loader,
                bundle_assembler=_assembler,
                harness_fn=_harness,
            ),
        )
    )

    assert call_order == ["qualitative", "bundle", "harness"]


# ---------------------------------------------------------------------------
# Test 8: All three sectors produce valid results
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sector", list(Sector))
def test_all_sectors_produce_result(sector: Sector) -> None:
    """run_domain_researcher succeeds for every Sector value."""
    result = asyncio.run(
        _run_domain_researcher(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_output_text=f"distillation text for {sector}",
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            deps=_make_deps(sector),
        )
    )

    assert result.sector == sector
    assert result.brief.sector == sector
    assert result.retry_count == 0
    assert result.tokens_used.input_tokens == 100
    assert result.wall_clock_seconds >= 0
