"""End-to-end live integration test for the adaptive researcher (ALP-265).

Skipped by default. Set ``RUN_LIVE_LLM_TESTS=1`` and provide a live
``CLAUDE_CODE_OAUTH_TOKEN`` to run against the populated production
database with the real Claude Agent SDK.

Per parent issue § Pre-resolved configuration decisions (H), this story
fakes the upstream pipeline composition by loading recorded fixtures
``(tuple[SectorBrief, ...], QualitativeBrief, CorrelationRegimeBrief,
DistillationOutputs, universal_regime_label)`` rather than running
domain researchers + qualitative + distillation live. Live composition
belongs in the separate execution-layer wiring story.

The test asserts the structural invariants spelled out in story ALP-265
§ Acceptance criteria:

- The runner returns an :class:`AdaptiveResearcherResult` with a typed
  :class:`AdaptiveBrief` (no exceptions, no parse/validation failures
  from the harness).
- Every ``Strengthens`` / ``Weakens`` reference in every SIGNAL thread
  resolves against the fixture upstream IDs (no Layer-3 violations).
- The diagnostic archive populates ``prompt.md``, ``user_message.md``,
  ``response_initial.md``, ``errors.json``, ``metadata.json`` under
  ``<archive_root>/invocations/<invocation_id>/analysis/adaptive_researcher/``.
- The rendered ``input_bundle.bundle_text`` contains all four section
  markers (header + regime + distillation + sector).
- Every ``tools_used`` entry in every thread (when threads exist) is in
  the agent's registered tool allowlist.

The harness's no-SDK-required paths (parser, validator, retry) are
covered by ``tests/analysis/adaptive_research/test_harness.py``; this
test exercises the live path only.

The non-e2e portions (fixture loading + structural fixture assertions)
run in every pytest invocation because they don't touch the SDK.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind.analysis.adaptive_research.models import Assessment
from alphamind.analysis.adaptive_research.runner import (
    AdaptiveResearcherResult,
    run_adaptive_researcher,
)
from alphamind.analysis.adaptive_research.validation import _build_reference_universe
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.config.models.agents import AdaptiveAgentConfig, AgentName
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._common import load_universe_scope
from alphamind.scripts.verify_adaptive_researcher import (
    _REQUIRED_DIAGNOSTIC_FILES,
    _diagnostic_dir,
    _load_agents_config,
)
from tests.analysis.adaptive_research.fixtures import load_e2e_fixtures

_LIVE_FLAG = "RUN_LIVE_LLM_TESTS"
_OAUTH_FLAG = "CLAUDE_CODE_OAUTH_TOKEN"

# All four section markers the input-bundle assembler emits.
_INPUT_BUNDLE_SECTION_MARKERS: tuple[str, ...] = (
    "=== ADAPTIVE RESEARCH INPUT",
    "=== VOLATILITY REGIME ===",
    "=== DISTILLATION ANOMALY FLAGS",
    "=== SECTOR-RESEARCHER ANOMALIES",
)


# ---------------------------------------------------------------------------
# Non-e2e: fixture-loading and structural-fixture assertions
#
# These run in every pytest invocation; they do not touch the SDK.
# ---------------------------------------------------------------------------


def test_load_e2e_fixtures_returns_typed_tuple() -> None:
    """``load_e2e_fixtures()`` returns the typed five-tuple the runner consumes."""
    fixtures = load_e2e_fixtures()
    sector_briefs, qualitative_brief, correlation_regime_brief, distillation_outputs, regime = (
        fixtures
    )

    assert isinstance(sector_briefs, tuple)
    assert len(sector_briefs) == 3
    for sb in sector_briefs:
        assert isinstance(sb, SectorBrief)
    assert isinstance(qualitative_brief, QualitativeBrief)
    assert isinstance(correlation_regime_brief, CorrelationRegimeBrief)
    assert isinstance(distillation_outputs, DistillationOutputs)
    assert isinstance(regime, dict)


def test_fixtures_exercise_validator_layer3_universe() -> None:
    """Fixtures populate every Layer-3 prefix family the validator resolves.

    The reference universe must include at least one ID for each prefix
    family:

    * ``SA-{SECTOR}-N`` from sector findings
    * ``SA-{SECTOR}-ANOM-N`` from sector anomalies
    * ``SA-{SECTOR}-TC-N`` from sector thesis candidates
    * ``QR-N`` from qualitative threads
    * ``QR-CW-N`` from qualitative catalyst watches
    * ``CR-N`` from correlation/regime reference index
    """
    sector_briefs, qb, crb, _do, _regime = load_e2e_fixtures()
    valid_ids = _build_reference_universe(sector_briefs, qb, crb)
    assert any(
        rid.startswith("SA-") and "-ANOM-" not in rid and "-TC-" not in rid for rid in valid_ids
    )
    assert any("-ANOM-" in rid for rid in valid_ids)
    assert any("-TC-" in rid for rid in valid_ids)
    assert any(rid.startswith("QR-") and not rid.startswith("QR-CW-") for rid in valid_ids)
    assert any(rid.startswith("QR-CW-") for rid in valid_ids)
    assert any(rid.startswith("CR-") for rid in valid_ids)


def test_distillation_outputs_carries_mixed_anomaly_blocks() -> None:
    """Distillation fixture is multi-block with at least one anomaly flag.

    Drives the renderer's ``DISTILLATION ANOMALY FLAGS`` section out of
    the ``(none)`` branch so the live e2e bundle exercises real flag
    rendering.
    """
    _sb, _qb, _crb, distillation_outputs, _regime = load_e2e_fixtures()
    assert len(distillation_outputs.all_blocks) >= 2
    flag_total = sum(len(block.anomaly_flags) for block in distillation_outputs.all_blocks)
    assert flag_total >= 1


def test_universal_regime_label_carries_canonical_keys() -> None:
    """Regime fixture populates the four keys the input-bundle renderer reads."""
    _sb, _qb, _crb, _do, regime = load_e2e_fixtures()
    expected = {"regime", "transition_flag", "confidence", "freshness_ts"}
    assert expected.issubset(regime.keys())


# ---------------------------------------------------------------------------
# E2E live test (gated)
# ---------------------------------------------------------------------------


def _resolve_archive_root() -> Path:
    """Use a per-run temp dir under the repo's .archive/ for traceability.

    A fresh subdirectory keeps the diagnostic-archive assertions
    deterministic across multiple invocations of the test.
    """
    repo_root = Path(__file__).resolve().parents[3]
    base = repo_root / ".archive" / "test-e2e-adaptive-researcher"
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get(_LIVE_FLAG) != "1",
    reason=f"Live LLM test gated on {_LIVE_FLAG}=1; skipped by default.",
)
def test_adaptive_researcher_e2e_live() -> None:
    """Run the adaptive researcher against fixture upstream + live SDK + populated DB."""
    if not os.environ.get(_OAUTH_FLAG):
        pytest.skip(f"{_OAUTH_FLAG} not set — required for live SDK invocation.")

    sector_briefs, qb, crb, distillation_outputs, regime_label = load_e2e_fixtures()

    now = datetime.now(tz=UTC)
    invocation_id = now.strftime("%Y%m%dT%H%M%SZ") + "-e2e-adaptive-researcher"
    archive_root = _resolve_archive_root()
    universe = frozenset(load_universe_scope())

    agents_yaml = _load_agents_config()
    agent_config = agents_yaml.agents[AgentName.adaptive_researcher]
    assert isinstance(agent_config, AdaptiveAgentConfig), (
        "adaptive_researcher must be a tool-loop agent"
    )
    registered_tool_ids = frozenset(agent_config.tools)
    agents_config = {name.value: cfg for name, cfg in agents_yaml.agents.items()}

    engine = make_engine()
    factory = make_session_factory(engine)
    with factory() as session:
        result: AdaptiveResearcherResult = asyncio.run(
            run_adaptive_researcher(
                invocation_id=invocation_id,
                as_of=now,
                session=session,
                distillation_outputs=distillation_outputs,
                sector_briefs=sector_briefs,
                qualitative_brief=qb,
                correlation_regime_brief=crb,
                universal_regime_label=regime_label,
                universe=universe,
                agents_config=agents_config,
                archive_root=archive_root,
            )
        )

    # Bundle exercised the all-four-sections code path.
    bundle_text = result.input_bundle.bundle_text
    for marker in _INPUT_BUNDLE_SECTION_MARKERS:
        assert marker in bundle_text, f"input bundle missing section marker {marker!r}"

    # Layer-3 referential resolution against the fixture upstream IDs.
    valid_ids: frozenset[str] = _build_reference_universe(sector_briefs, qb, crb)
    for thread in result.brief.threads:
        if thread.assessment is not Assessment.SIGNAL:
            continue
        for ref in thread.strengthens or ():
            assert ref in valid_ids, (
                f"thread {thread.thread_id} Strengthens ref {ref!r} does not resolve "
                f"against fixture upstream IDs"
            )
        for ref in thread.weakens or ():
            assert ref in valid_ids, (
                f"thread {thread.thread_id} Weakens ref {ref!r} does not resolve "
                f"against fixture upstream IDs"
            )

    # Tools-used in each thread comes from the agent's allowlist only.
    for thread in result.brief.threads:
        for tool in thread.tools_used:
            assert tool in registered_tool_ids, (
                f"thread {thread.thread_id} cites tool {tool!r} not in the registered "
                f"allowlist {sorted(registered_tool_ids)!r}"
            )

    # Diagnostic archive populated.
    diag_dir = _diagnostic_dir(archive_root, invocation_id, AgentName.adaptive_researcher.value)
    missing = [name for name in _REQUIRED_DIAGNOSTIC_FILES if not (diag_dir / name).exists()]
    assert not missing, f"diagnostic archive {diag_dir} missing files: {missing}"

    # Sanity: typed AdaptiveBrief returned (parse + validate succeeded).
    assert isinstance(result.brief.invocation_id, str)
    assert result.brief.invocation_id
