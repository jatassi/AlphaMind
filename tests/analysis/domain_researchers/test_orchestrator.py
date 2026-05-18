"""Tests for the parallel domain-researcher orchestrator — story 11 (ALP-190).

The orchestrator fans out three per-sector ``run_domain_researcher`` calls
under ``asyncio.gather(..., return_exceptions=False)``, propagates the
first failure under fail-closed semantics, and aggregates the three results
into a :class:`DomainResearchersOutput` value object the synthesizer
consumes downstream.

All tests stub the runner — no real SDK calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.harness import MalformedOutputFailure
from alphamind.analysis.domain_researchers.input_bundle import InputBundle
from alphamind.analysis.domain_researchers.models import SECTOR_PREFIX, SectorBrief, SignalQuality
from alphamind.analysis.domain_researchers.orchestrator import (
    DomainResearchersOutput,
    _run_domain_researchers,
    run_domain_researchers,
)
from alphamind.analysis.domain_researchers.runner import DomainResearcherResult
from alphamind.config.models.agents import AgentName, AllowedModel, BaseAgentConfig
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.sector_assembly import SectorOutput

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "test-inv-001"


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
                headline=f"Test finding for {sector.value}",
                tickers=("AAA",),
                signal_type=SignalType.PRICE_ACTION,
                strength=Strength.STRONG,
                detail="Test detail.",
            ),
        ),
        anomalies=(),
        thesis_candidates=(),
    )


def _make_input_bundle(sector: Sector) -> InputBundle:
    return InputBundle(
        sector=sector,
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        distillation_text=f"distillation text for {sector.value}",
        qualitative_text="qual text",
        bundle_text=f"bundle text for {sector.value}",
    )


def _make_runner_result(
    sector: Sector,
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    wall_clock_seconds: float = 1.0,
    retry_count: int = 0,
) -> DomainResearcherResult:
    return DomainResearcherResult(
        sector=sector,
        brief=_make_sector_brief(sector),
        input_bundle=_make_input_bundle(sector),
        tokens_used=TokensUsed(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        wall_clock_seconds=wall_clock_seconds,
        retry_count=retry_count,
    )


def _make_sector_output(audience: OutputAudience, *, text: str) -> SectorOutput:
    return SectorOutput(
        audience=audience,
        sector_label=audience.value,
        text=text,
        tickers=(),
        block_ids=(),
        freshness_min=_AS_OF,
    )


def _make_distillation_outputs(
    *,
    tech_text: str = "TECH-DISTILL",
    fin_text: str = "FIN-DISTILL",
    energy_text: str = "ENERGY-DISTILL",
) -> DistillationOutputs:
    """Build a minimal DistillationOutputs whose `sector_outputs` map covers
    the three sector audiences and `correlation_regime_brief` is a stub."""
    from alphamind.distillation.correlation_brief import CorrelationRegimeBrief

    return DistillationOutputs(
        sector_outputs={
            OutputAudience.SECTOR_TECH_SEMIS: _make_sector_output(
                OutputAudience.SECTOR_TECH_SEMIS, text=tech_text
            ),
            OutputAudience.SECTOR_FINANCIALS: _make_sector_output(
                OutputAudience.SECTOR_FINANCIALS, text=fin_text
            ),
            OutputAudience.SECTOR_ENERGY: _make_sector_output(
                OutputAudience.SECTOR_ENERGY, text=energy_text
            ),
        },
        correlation_regime_brief=CorrelationRegimeBrief(
            text="stub", reference_index={}, freshness_min=_AS_OF
        ),
        universal_regime_label={"label": "calm"},
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        total_blocks=0,
        total_anomalies=0,
        non_calibrated_block_count=0,
        all_blocks=(),
    )


def _make_agents_registry() -> dict[str, BaseAgentConfig]:
    cfg = BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/tech_semis_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )
    return {
        AgentName.tech_semis_researcher.value: cfg,
        AgentName.financials_researcher.value: cfg,
        AgentName.energy_researcher.value: cfg,
    }


def _make_sectors_registry() -> dict[str, list[str]]:
    return {
        Sector.TECH_SEMIS.value: ["NVDA"],
        Sector.FINANCIALS.value: ["JPM"],
        Sector.ENERGY.value: ["XOM"],
    }


# ---------------------------------------------------------------------------
# Stub runner factories
# ---------------------------------------------------------------------------


def _runner_returning(
    by_sector: dict[Sector, DomainResearcherResult],
) -> Callable[..., Awaitable[DomainResearcherResult]]:
    """Build an async runner stub that returns canned results keyed on sector."""

    async def _runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        **_kw: Any,
    ) -> DomainResearcherResult:
        return by_sector[sector]

    return _runner


# ---------------------------------------------------------------------------
# Test 1: Happy path — orchestrator returns a DomainResearchersOutput
# ---------------------------------------------------------------------------


def test_run_domain_researchers_returns_output() -> None:
    """All three runners succeed → DomainResearchersOutput is returned with
    each sector's result placed in its positional field."""
    by_sector = {
        Sector.TECH_SEMIS: _make_runner_result(Sector.TECH_SEMIS),
        Sector.FINANCIALS: _make_runner_result(Sector.FINANCIALS),
        Sector.ENERGY: _make_runner_result(Sector.ENERGY),
    }
    output = asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner_returning(by_sector),
        )
    )
    assert isinstance(output, DomainResearchersOutput)
    assert output.invocation_id == _INVOCATION_ID
    assert output.as_of == _AS_OF
    assert output.tech_semis is by_sector[Sector.TECH_SEMIS]
    assert output.financials is by_sector[Sector.FINANCIALS]
    assert output.energy is by_sector[Sector.ENERGY]


# ---------------------------------------------------------------------------
# Test 2: Token aggregation is field-wise sum across the three sectors
# ---------------------------------------------------------------------------


def test_token_aggregation_is_field_wise_sum() -> None:
    by_sector = {
        Sector.TECH_SEMIS: _make_runner_result(
            Sector.TECH_SEMIS,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
        ),
        Sector.FINANCIALS: _make_runner_result(
            Sector.FINANCIALS,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
        ),
        Sector.ENERGY: _make_runner_result(
            Sector.ENERGY,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
        ),
    }
    output = asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner_returning(by_sector),
        )
    )
    assert output.total_tokens_used.input_tokens == 300
    assert output.total_tokens_used.output_tokens == 150
    assert output.total_tokens_used.cache_read_tokens == 30
    assert output.total_tokens_used.cache_write_tokens == 15


# ---------------------------------------------------------------------------
# Test 3: Wall-clock aggregation is max() not sum (parallel execution)
# ---------------------------------------------------------------------------


def test_wall_clock_aggregation_is_max() -> None:
    by_sector = {
        Sector.TECH_SEMIS: _make_runner_result(Sector.TECH_SEMIS, wall_clock_seconds=5.0),
        Sector.FINANCIALS: _make_runner_result(Sector.FINANCIALS, wall_clock_seconds=12.0),
        Sector.ENERGY: _make_runner_result(Sector.ENERGY, wall_clock_seconds=8.0),
    }
    output = asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner_returning(by_sector),
        )
    )
    assert output.total_wall_clock_seconds == 12.0


# ---------------------------------------------------------------------------
# Test 4: Retry-count aggregation is sum across the three sectors
# ---------------------------------------------------------------------------


def test_retry_count_aggregation_is_sum() -> None:
    by_sector = {
        Sector.TECH_SEMIS: _make_runner_result(Sector.TECH_SEMIS, retry_count=0),
        Sector.FINANCIALS: _make_runner_result(Sector.FINANCIALS, retry_count=1),
        Sector.ENERGY: _make_runner_result(Sector.ENERGY, retry_count=0),
    }
    output = asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner_returning(by_sector),
        )
    )
    assert output.total_retry_count == 1


# ---------------------------------------------------------------------------
# Test 5: One-sector failure propagates; no partial result returned
# ---------------------------------------------------------------------------


def test_one_sector_failure_propagates() -> None:
    """A HarnessFailure in one sector aborts the orchestrator without returning
    a partial result."""
    failure = MalformedOutputFailure(
        "parse failed",
        agent_name=AgentName.financials_researcher.value,
        invocation_id=_INVOCATION_ID,
    )

    async def _runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        **_kw: Any,
    ) -> DomainResearcherResult:
        if sector is Sector.FINANCIALS:
            raise failure
        return _make_runner_result(sector)

    with pytest.raises(MalformedOutputFailure) as exc_info:
        asyncio.run(
            _run_domain_researchers(
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                distillation_outputs=_make_distillation_outputs(),
                agents_config=_make_agents_registry(),
                sectors_config=_make_sectors_registry(),
                runner_fn=_runner,
            )
        )
    assert exc_info.value is failure


# ---------------------------------------------------------------------------
# Test 6: When one sector fails, in-flight coroutines are cancelled or finish
# (i.e., the orchestrator does not swallow the failure or wait forever)
# ---------------------------------------------------------------------------


def test_two_sector_failures_propagate_via_exception_group_unwrap() -> None:
    """Two sectors fail simultaneously → orchestrator surfaces one failure
    (the first child of the underlying ``BaseExceptionGroup``) so callers
    see the same exception type they'd see with a single failure.

    Guards the TaskGroup migration: ``asyncio.TaskGroup`` always raises
    ``BaseExceptionGroup``; without explicit unwrapping the caller would
    suddenly receive a group container instead of a ``HarnessFailure``.
    """
    first_failure = MalformedOutputFailure(
        "first parse failed",
        agent_name=AgentName.tech_semis_researcher.value,
        invocation_id=_INVOCATION_ID,
    )
    second_failure = MalformedOutputFailure(
        "second parse failed",
        agent_name=AgentName.financials_researcher.value,
        invocation_id=_INVOCATION_ID,
    )

    async def _runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        **_kw: Any,
    ) -> DomainResearcherResult:
        # Yield so both runners start before either raises.
        await asyncio.sleep(0)
        if sector is Sector.TECH_SEMIS:
            raise first_failure
        if sector is Sector.FINANCIALS:
            raise second_failure
        return _make_runner_result(sector)

    with pytest.raises(MalformedOutputFailure) as exc_info:
        asyncio.run(
            _run_domain_researchers(
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                distillation_outputs=_make_distillation_outputs(),
                agents_config=_make_agents_registry(),
                sectors_config=_make_sectors_registry(),
                runner_fn=_runner,
            )
        )
    # The surfaced exception must be one of the original failures, not a
    # synthetic wrapper. The original ``__cause__`` chain may carry the
    # ``BaseExceptionGroup`` for diagnostic preservation, but the leaf must
    # be the canonical ``HarnessFailure`` subclass.
    assert exc_info.value in (first_failure, second_failure)


def test_one_sector_failure_does_not_block_on_in_flight_coroutines() -> None:
    """When one sector raises, the orchestrator does not hang waiting on the
    other two — TaskGroup cancels in-flight coroutines as soon as one raises,
    so the remaining work either completes quickly (already returned) or is
    cancelled."""

    started: dict[Sector, bool] = {}

    async def _runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        **_kw: Any,
    ) -> DomainResearcherResult:
        started[sector] = True
        if sector is Sector.TECH_SEMIS:
            # Fail immediately
            raise MalformedOutputFailure(
                "parse failed",
                agent_name=AgentName.tech_semis_researcher.value,
                invocation_id=_INVOCATION_ID,
            )
        # The other two never resolve unless cancelled
        await asyncio.sleep(60)
        return _make_runner_result(sector)

    async def _drive() -> None:
        with pytest.raises(MalformedOutputFailure):
            await _run_domain_researchers(
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                distillation_outputs=_make_distillation_outputs(),
                agents_config=_make_agents_registry(),
                sectors_config=_make_sectors_registry(),
                runner_fn=_runner,
            )

    # If the orchestrator did not use return_exceptions=False (or a strict
    # equivalent), the never-resolving siblings would deadlock the test.
    # asyncio.run will return promptly because gather cancels them.
    asyncio.run(asyncio.wait_for(_drive(), timeout=5.0))


# ---------------------------------------------------------------------------
# Test 7: Each runner receives the matching sector's distillation_text
# ---------------------------------------------------------------------------


def test_each_runner_receives_matching_distillation_text() -> None:
    """The orchestrator extracts ``distillation_outputs.sector_outputs[<audience>].text``
    for each sector and passes it to that sector's runner."""
    received: dict[Sector, str] = {}

    async def _runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        **_kw: Any,
    ) -> DomainResearcherResult:
        received[sector] = distillation_output_text
        return _make_runner_result(sector)

    asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(
                tech_text="TECH-DISTILL-X",
                fin_text="FIN-DISTILL-Y",
                energy_text="ENERGY-DISTILL-Z",
            ),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner,
        )
    )
    assert received[Sector.TECH_SEMIS] == "TECH-DISTILL-X"
    assert received[Sector.FINANCIALS] == "FIN-DISTILL-Y"
    assert received[Sector.ENERGY] == "ENERGY-DISTILL-Z"


# ---------------------------------------------------------------------------
# Test 8: Positional fields match runner outputs deterministically
# ---------------------------------------------------------------------------


def test_positional_fields_match_runner_outputs_deterministically() -> None:
    """Even if the runner async stubs resolve in arbitrary order, the
    orchestrator's positional fields (`tech_semis`, `financials`, `energy`)
    each carry the expected sector's result."""
    # Assign distinct, sector-tagged retry counts so we can verify mapping
    by_sector = {
        Sector.TECH_SEMIS: _make_runner_result(Sector.TECH_SEMIS, retry_count=10),
        Sector.FINANCIALS: _make_runner_result(Sector.FINANCIALS, retry_count=20),
        Sector.ENERGY: _make_runner_result(Sector.ENERGY, retry_count=30),
    }

    # Deliberately shuffle resolution order
    async def _runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        **_kw: Any,
    ) -> DomainResearcherResult:
        delay = {Sector.TECH_SEMIS: 0.03, Sector.FINANCIALS: 0.01, Sector.ENERGY: 0.02}[sector]
        await asyncio.sleep(delay)
        return by_sector[sector]

    output = asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner,
        )
    )
    assert output.tech_semis.retry_count == 10
    assert output.financials.retry_count == 20
    assert output.energy.retry_count == 30


# ---------------------------------------------------------------------------
# Test 9: DomainResearchersOutput is frozen
# ---------------------------------------------------------------------------


def test_output_is_frozen() -> None:
    """ALP-474: DomainResearchersOutput is a frozen dataclass."""
    import dataclasses

    by_sector = {
        Sector.TECH_SEMIS: _make_runner_result(Sector.TECH_SEMIS),
        Sector.FINANCIALS: _make_runner_result(Sector.FINANCIALS),
        Sector.ENERGY: _make_runner_result(Sector.ENERGY),
    }
    output = asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner_returning(by_sector),
        )
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        output.tech_semis = by_sector[Sector.ENERGY]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 10: Public entry point delegates to the private one with the
# production runner.
# ---------------------------------------------------------------------------


def test_public_entry_point_invokes_real_runner_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_domain_researchers`` (no ``runner_fn`` injected) should call
    ``run_domain_researcher`` for each sector. We monkey-patch the runner
    in the orchestrator module to assert it gets called three times."""
    calls: list[Sector] = []

    async def _fake_runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        **_kw: Any,
    ) -> DomainResearcherResult:
        calls.append(sector)
        return _make_runner_result(sector)

    monkeypatch.setattr(
        "alphamind.analysis.domain_researchers.orchestrator.run_domain_researcher",
        _fake_runner,
    )
    output = asyncio.run(
        run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            session=None,  # type: ignore[arg-type]
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
        )
    )

    assert sorted(calls, key=lambda s: s.value) == sorted(
        [Sector.TECH_SEMIS, Sector.FINANCIALS, Sector.ENERGY], key=lambda s: s.value
    )
    assert isinstance(output, DomainResearchersOutput)


# ---------------------------------------------------------------------------
# Test 11: archive_root is forwarded to each runner call (diagnostic preservation)
# ---------------------------------------------------------------------------


def test_archive_root_is_forwarded_to_each_runner(tmp_path: Any) -> None:
    """The orchestrator forwards ``archive_root`` to each per-sector runner so
    the harness's diagnostic-archive write is enabled even on aborted invocations."""
    received: dict[Sector, Any] = {}

    async def _runner(
        sector: Sector,
        invocation_id: str,
        as_of: datetime,
        distillation_output_text: str,
        *,
        archive_root: Any = None,
        **_kw: Any,
    ) -> DomainResearcherResult:
        received[sector] = archive_root
        return _make_runner_result(sector)

    asyncio.run(
        _run_domain_researchers(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            agents_config=_make_agents_registry(),
            sectors_config=_make_sectors_registry(),
            runner_fn=_runner,
            archive_root=tmp_path,
        )
    )
    assert received[Sector.TECH_SEMIS] == tmp_path
    assert received[Sector.FINANCIALS] == tmp_path
    assert received[Sector.ENERGY] == tmp_path
